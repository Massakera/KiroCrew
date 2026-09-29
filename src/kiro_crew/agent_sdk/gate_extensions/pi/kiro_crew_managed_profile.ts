/**
 * Opt-in Crew-owned pi resource and tool policy.
 *
 * Loaded before the unchanged permission gate, from a separately sealed copy.
 * It can narrow tools, never approve them: every allowed call still reaches the
 * permission gate. Its own command-registry read-back is required at startup.
 */
import { dirname, resolve } from "node:path";
import { getAgentDir, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

export function managedTools(raw: string | undefined): Set<string> {
  if (raw === undefined) throw new Error("Kiro Crew managed pi: missing tool projection");
  const tools: unknown = JSON.parse(raw);
  if (!Array.isArray(tools) || tools.some((tool) => typeof tool !== "string" || !tool)) {
    throw new Error("Kiro Crew managed pi: invalid tool projection");
  }
  return new Set(tools);
}

export default function (pi: ExtensionAPI) {
  const managed = managedTools(process.env.KIROCREW_PI_MANAGED_TOOLS);
  pi.on("session_start", () => {
    pi.setActiveTools([...managed]);
  });
  pi.on("before_agent_start", (event) => {
    const options = event.systemPromptOptions;
    // Use pi's normalization, including file URLs and Windows path spellings.
    // Keep repository/ancestor context; Crew supplies its own agent and skills.
    const agentDir = resolve(getAgentDir());
    options.contextFiles = options.contextFiles.filter(
      (file) => dirname(resolve(file.path)) !== agentDir,
    );
    options.customPrompt = undefined;
    options.forceSystemPrompt = undefined;
    options.appendSystemPrompt = "";
    options.sections.crew_execution =
      "Kiro Crew owns this session's agent profile and delegation. " +
      "Use Crew's spawn tools when granted; do not launch a separate subagent runtime. " +
      "The active tool list is the selected Crew agent's contract.";
  });
  pi.on("tool_call", (event) => {
    if (!managed.has(String(event.toolName))) {
      return { block: true, reason: "Tool is not granted by the Kiro Crew agent profile" };
    }
    return undefined;
  });
  pi.registerCommand("kiro-crew-managed", {
    description: "Kiro Crew's managed pi profile is loaded in this session",
    handler: async (_args, ctx) => {
      ctx.ui.notify("Kiro Crew managed pi profile: active", "info");
    },
  });
}
