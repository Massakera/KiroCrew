import { build } from '../../website/node_modules/esbuild/lib/main.js'
import { fileURLToPath } from 'node:url'

await build({
  absWorkingDir: fileURLToPath(new URL('.', import.meta.url)),
  entryPoints: ['ui/src/App.tsx'],
  outfile: 'ui/dist/index.mjs',
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: 'es2022',
  jsx: 'transform',
  minify: true,
  external: ['react', '@kirocrew/app-sdk', '@kirocrew/app-sdk/ui', '@tanstack/react-query', 'lucide-react'],
})
