import { build } from "esbuild";

await build({
	entryPoints: ["agent.mjs"],
	bundle: true,
	platform: "node",
	target: "node22",
	format: "esm",
	minify: true,
	legalComments: "none",
	outfile: "dist/agent.mjs",
	banner: { js: "import{createRequire}from'node:module';const require=createRequire(import.meta.url);" },
});
