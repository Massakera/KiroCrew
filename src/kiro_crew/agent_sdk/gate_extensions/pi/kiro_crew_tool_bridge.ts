/**
 * Kiro Crew tool bridge for the pi coding agent.
 *
 * pi-acp accepts the session's `mcpServers` array and never hands it to pi, so a
 * pi session holds none of Kiro Crew's own tools. This extension is the channel
 * that does reach pi: Kiro Crew places the session's broker stub elements in this
 * process's environment, and the extension speaks MCP over stdio to each of them
 * and registers the tools Kiro Crew named as pi tools.
 *
 * A registered tool is named `mcp__<server>__<tool>`, the spelling Kiro Crew's
 * policy already matches for an MCP tool. Every call still fires pi's
 * `tool_call` event, so Kiro Crew's gate extension asks about it like any other
 * tool; the gate reports the file a tool came from, and Kiro Crew treats the call
 * as that MCP tool only when that file is the sealed copy of THIS extension.
 *
 * The stubs talk to Kiro Crew's broker, which runs outside the sandbox and binds
 * each call to this session. Nothing here reads a credential: the session token
 * the stubs present is already in the environment they inherit.
 *
 * With no server list in the environment (the gate read-back, or a session whose
 * broker does not stub Kiro Crew's server) the extension registers its probe
 * command and nothing else.
 */

import { spawn, type ChildProcess } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** Registered so the host can see this file loaded. */
const PROBE_COMMAND = "kiro-crew-bridge";

/** JSON the host writes: `{"servers": [{name, command, args, env, tools}]}`. */
const SERVERS_ENV = "KIROCREW_PI_BRIDGE_SERVERS";

const PROTOCOL_VERSION = "2025-06-18";
const HANDSHAKE_TIMEOUT_MS = 15000;
const STDERR_TAIL_CHARS = 2000;

type ServerSpec = {
  name: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  tools: string[];
};

type McpTool = { name: string; description?: string; inputSchema?: unknown };

type Pending = { resolve: (value: any) => void; reject: (error: Error) => void };

function parseServers(raw: string): ServerSpec[] {
  const body = JSON.parse(raw);
  const servers = Array.isArray(body?.servers) ? body.servers : [];
  const out: ServerSpec[] = [];
  for (const s of servers) {
    if (!s || typeof s.name !== "string" || typeof s.command !== "string") continue;
    const env: Record<string, string> = {};
    if (s.env && typeof s.env === "object") {
      for (const [k, v] of Object.entries(s.env)) {
        if (typeof v === "string") env[k] = v;
      }
    }
    out.push({
      name: s.name,
      command: s.command,
      args: Array.isArray(s.args) ? s.args.filter((a: unknown) => typeof a === "string") : [],
      env,
      tools: Array.isArray(s.tools) ? s.tools.filter((t: unknown) => typeof t === "string") : [],
    });
  }
  return out;
}

/** One stdio MCP server: newline-delimited JSON-RPC, restarted on demand after it exits. */
export class StdioMcpClient {
  private proc: ChildProcess | undefined;
  private ready: Promise<void> | undefined;
  private buffer = "";
  private stderrTail = "";
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private readonly spec: ServerSpec;

  constructor(spec: ServerSpec) {
    this.spec = spec;
  }

  /** Start the server and complete the MCP handshake, once per process lifetime. */
  connect(): Promise<void> {
    if (!this.ready) {
      this.ready = this.start().catch((error) => {
        this.ready = undefined;
        this.stop();
        throw error;
      });
    }
    return this.ready;
  }

  async listTools(): Promise<McpTool[]> {
    await this.connect();
    const tools: McpTool[] = [];
    let cursor: string | undefined;
    do {
      const result = await this.request(
        "tools/list",
        cursor ? { cursor } : {},
        undefined,
        HANDSHAKE_TIMEOUT_MS,
      );
      for (const t of Array.isArray(result?.tools) ? result.tools : []) {
        if (t && typeof t.name === "string") tools.push(t);
      }
      cursor = typeof result?.nextCursor === "string" && result.nextCursor ? result.nextCursor : undefined;
    } while (cursor);
    return tools;
  }

  async callTool(name: string, args: unknown, signal?: AbortSignal): Promise<any> {
    await this.connect();
    return this.request("tools/call", { name, arguments: args ?? {} }, signal);
  }

  stop(): void {
    const proc = this.proc;
    this.proc = undefined;
    this.ready = undefined;
    if (proc && proc.exitCode === null) proc.kill();
    this.failPending(new Error(`Kiro Crew server ${this.spec.name} stopped`));
  }

  private async start(): Promise<void> {
    const proc = spawn(this.spec.command, this.spec.args, {
      env: { ...process.env, ...this.spec.env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.proc = proc;
    this.buffer = "";
    this.stderrTail = "";
    proc.stdout?.setEncoding("utf8");
    proc.stdout?.on("data", (chunk: string) => this.onData(chunk));
    proc.stderr?.setEncoding("utf8");
    proc.stderr?.on("data", (chunk: string) => {
      this.stderrTail = (this.stderrTail + chunk).slice(-STDERR_TAIL_CHARS);
    });
    const gone = (detail: string) => {
      if (this.proc !== proc) return;
      this.proc = undefined;
      this.ready = undefined;
      const tail = this.stderrTail.trim();
      this.failPending(
        new Error(`Kiro Crew server ${this.spec.name} ${detail}${tail ? `: ${tail}` : ""}`),
      );
    };
    proc.on("error", (error) => gone(`could not start (${error.message})`));
    proc.on("exit", (code, sig) => gone(`exited (${sig ?? code})`));
    await this.request(
      "initialize",
      {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: "kiro-crew-pi-bridge", version: "1" },
      },
      undefined,
      HANDSHAKE_TIMEOUT_MS,
    );
    this.send({ jsonrpc: "2.0", method: "notifications/initialized" });
  }

  private request(
    method: string,
    params: unknown,
    signal?: AbortSignal,
    timeoutMs?: number,
  ): Promise<any> {
    const proc = this.proc;
    if (!proc) {
      return Promise.reject(new Error(`Kiro Crew server ${this.spec.name} is not running`));
    }
    if (signal?.aborted) return Promise.reject(new Error("Aborted"));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const onAbort = () => {
        this.send({
          jsonrpc: "2.0",
          method: "notifications/cancelled",
          params: { requestId: id, reason: "aborted" },
        });
        settle();
        reject(new Error("Aborted"));
      };
      const settle = () => {
        this.pending.delete(id);
        if (timer) clearTimeout(timer);
        signal?.removeEventListener("abort", onAbort);
      };
      this.pending.set(id, {
        resolve: (value) => {
          settle();
          resolve(value);
        },
        reject: (error) => {
          settle();
          reject(error);
        },
      });
      signal?.addEventListener("abort", onAbort, { once: true });
      if (timeoutMs) {
        timer = setTimeout(() => {
          settle();
          reject(new Error(`Kiro Crew server ${this.spec.name}: ${method} timed out`));
        }, timeoutMs);
      }
      this.send({ jsonrpc: "2.0", id, method, params });
    });
  }

  private send(message: unknown): void {
    const stdin = this.proc?.stdin;
    if (stdin && !stdin.destroyed) stdin.write(`${JSON.stringify(message)}\n`);
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    let newline: number;
    while ((newline = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, newline).trim();
      this.buffer = this.buffer.slice(newline + 1);
      if (line) this.onMessage(line);
    }
  }

  private onMessage(line: string): void {
    let message: any;
    try {
      message = JSON.parse(line);
    } catch {
      return;
    }
    if (!message || typeof message !== "object") return;
    if (typeof message.method === "string") {
      // A request from the server (ping, roots/list, ...): this client offers no
      // capabilities, so every one is answered as unknown rather than left hanging.
      if (message.id !== undefined && message.id !== null) {
        this.send(
          message.method === "ping"
            ? { jsonrpc: "2.0", id: message.id, result: {} }
            : { jsonrpc: "2.0", id: message.id, error: { code: -32601, message: "Method not found" } },
        );
      }
      return;
    }
    const waiter = typeof message.id === "number" ? this.pending.get(message.id) : undefined;
    if (!waiter) return;
    if (message.error) {
      const text = typeof message.error.message === "string" ? message.error.message : "error";
      waiter.reject(new Error(`Kiro Crew server ${this.spec.name}: ${text}`));
    } else {
      waiter.resolve(message.result);
    }
  }

  private failPending(error: Error): void {
    const waiters = [...this.pending.values()];
    this.pending.clear();
    for (const waiter of waiters) waiter.reject(error);
  }
}

// Also the file's unit-test surface (driven directly under Node, no pi needed).
export { parseServers };

/** An MCP `tools/call` result in pi's tool-result shape. */
export function toPiContent(result: any): { type: string; [key: string]: unknown }[] {
  const out: { type: string; [key: string]: unknown }[] = [];
  for (const item of Array.isArray(result?.content) ? result.content : []) {
    if (item?.type === "text" && typeof item.text === "string") {
      out.push({ type: "text", text: item.text });
    } else if (item?.type === "image" && typeof item.data === "string") {
      out.push({ type: "image", data: item.data, mimeType: item.mimeType ?? "image/png" });
    } else if (item) {
      out.push({ type: "text", text: JSON.stringify(item) });
    }
  }
  if (out.length === 0 && result?.structuredContent !== undefined) {
    out.push({ type: "text", text: JSON.stringify(result.structuredContent) });
  }
  if (out.length === 0) out.push({ type: "text", text: "" });
  return out;
}

export function bridgedToolName(server: string, tool: string): string {
  return `mcp__${server}__${tool}`;
}

async function registerServer(pi: ExtensionAPI, spec: ServerSpec, client: StdioMcpClient) {
  const wanted = new Set(spec.tools);
  const listed = await client.listTools();
  let registered = 0;
  for (const tool of listed) {
    if (!wanted.has(tool.name)) continue;
    pi.registerTool({
      name: bridgedToolName(spec.name, tool.name),
      label: `Kiro Crew: ${tool.name}`,
      description: tool.description || tool.name,
      parameters: (tool.inputSchema ?? { type: "object", properties: {} }) as any,
      async execute(_toolCallId, params, signal) {
        const result = await client.callTool(tool.name, params, signal);
        const content = toPiContent(result);
        if (result?.isError) {
          const text = content
            .map((c) => (typeof c.text === "string" ? c.text : ""))
            .join("\n")
            .trim();
          throw new Error(text || `${tool.name} failed`);
        }
        return { content: content as any, details: result?.structuredContent ?? {} };
      },
    });
    registered++;
  }
  return registered;
}

export default async function (pi: ExtensionAPI) {
  const clients: StdioMcpClient[] = [];
  const status: string[] = [];

  pi.registerCommand(PROBE_COMMAND, {
    description: "Kiro Crew's tool bridge is loaded in this session",
    handler: async (_args, ctx) => {
      ctx.ui.notify(`Kiro Crew tool bridge: ${status.join("; ") || "no servers"}`, "info");
    },
  });

  pi.on("session_shutdown", async () => {
    for (const client of clients) client.stop();
  });

  const raw = process.env[SERVERS_ENV];
  if (!raw) return;
  let servers: ServerSpec[];
  try {
    servers = parseServers(raw);
  } catch (error) {
    process.stderr.write(`kiro-crew-bridge: unreadable server list (${String(error)})\n`);
    return;
  }
  for (const spec of servers) {
    const client = new StdioMcpClient(spec);
    clients.push(client);
    try {
      const count = await registerServer(pi, spec, client);
      status.push(`${spec.name}: ${count} tools`);
    } catch (error) {
      client.stop();
      const detail = error instanceof Error ? error.message : String(error);
      status.push(`${spec.name}: unavailable`);
      process.stderr.write(`kiro-crew-bridge: ${spec.name} unavailable (${detail})\n`);
    }
  }
}
