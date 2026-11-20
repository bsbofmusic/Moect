import {defineConfig} from 'vite'
import react from '@vitejs/plugin-react'
import tailwind from '@tailwindcss/vite'
import path from 'node:path'
import {readFileSync} from 'node:fs'

export default defineConfig({
  base: './',
  plugins: [react(), tailwind(), {
    name: 'moect-icons',
    apply: 'build',
    generateBundle() {
      for (const [fileName, sourcePath] of [['icon.svg', '../ui/icon.svg'], ['icon.ico', '../icon.ico']]) {
        this.emitFile({type: 'asset', fileName, source: readFileSync(new URL(sourcePath, import.meta.url))})
      }
    },
  }],
  resolve: {alias: {'@': path.resolve(import.meta.dirname)}},
  build: {outDir: '../ui-qce', emptyOutDir: true, target: 'chrome120'},
})
