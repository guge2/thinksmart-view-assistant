import { createInterface } from "node:readline";
import { Agent } from "@earendil-works/pi-agent-core";
import { streamSimple } from "@earendil-works/pi-ai/api/openai-completions";

const HISTORY_TURNS = 6;
const PROVIDER = "bailian";
const EMPTY_USAGE = {
	input: 0,
	output: 0,
	cacheRead: 0,
	cacheWrite: 0,
	totalTokens: 0,
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};

let config;
let agent;
let current = { id: 0, context: "", thinking: false, search: true, tool: null };
let queue = Promise.resolve();
const pendingTools = new Map();

function send(message) {
	process.stdout.write(`${JSON.stringify(message)}\n`);
}

function describeError(message) {
	const text = String(message || "unknown error");
	return /^\d{3}\b/.test(text) ? `HTTP ${text}` : text;
}

function callTool(toolCallId, name, args, signal) {
	return new Promise((resolve, reject) => {
		if (signal?.aborted) {
			reject(new Error("aborted"));
			return;
		}
		const onAbort = () => {
			pendingTools.delete(toolCallId);
			reject(new Error("aborted"));
		};
		signal?.addEventListener("abort", onAbort, { once: true });
		pendingTools.set(toolCallId, (result) => {
			signal?.removeEventListener("abort", onAbort);
			resolve(result);
		});
		send({ t: "tool", id: current.id, call: toolCallId, name, args });
	}).then((result) => ({
		content: [{ type: "text", text: result.text || "完成" }],
		details: undefined,
		isError: result.ok === false,
		terminate: result.final === true,
	}));
}

function setup(message) {
	config = message;
	const model = {
		id: message.model,
		name: message.model,
		api: "openai-completions",
		provider: PROVIDER,
		baseUrl: message.baseUrl,
		input: ["text"],
		reasoning: true,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 131072,
		maxTokens: message.maxTokens,
		compat: {
			thinkingFormat: "qwen",
			supportsDeveloperRole: false,
			supportsStore: false,
			supportsReasoningEffort: false,
			maxTokensField: "max_tokens",
		},
	};
	const tools = message.tools.map(({ function: spec }) => ({
		name: spec.name,
		label: spec.name,
		description: spec.description,
		parameters: spec.parameters,
		execute: (toolCallId, args, signal) => callTool(toolCallId, spec.name, args, signal),
	}));
	agent = new Agent({
		initialState: { systemPrompt: message.system, model, thinkingLevel: "off", tools },
		streamFn: (target, context, options) =>
			streamSimple(target, context, {
				...options,
				maxTokens: config.maxTokens,
				temperature: config.temperature,
				timeoutMs: 60000,
				maxRetries: 1,
			}),
		getApiKey: () => config.apiKey,
		transformContext: async (messages) => {
			const [head, ...rest] = messages;
			if (head?.role !== "system" || !current.context) return messages;
			return [{ ...head, content: `${config.system}${current.context}` }, ...rest];
		},
		onPayload: (params) => {
			const payload = { ...params, enable_thinking: current.thinking };
			delete payload.thinking_budget;
			if (current.thinking) payload.thinking_budget = config.thinkingBudget;
			// Only the first request of a turn is forced; after the tool result the model must be free to answer.
			if (current.tool && payload.messages?.at(-1)?.role === "user") {
				payload.tool_choice = { type: "function", function: { name: current.tool } };
			}
			if (config.search && current.search) {
				payload.enable_search = true;
				payload.search_options = { search_strategy: "turbo" };
			}
			return payload;
		},
		toolExecution: "sequential",
	});
	agent.subscribe((event) => {
		if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") {
			send({ t: "delta", id: current.id, text: event.assistantMessageEvent.delta });
		} else if (event.type === "tool_execution_end" && event.isError) {
			const text = event.result?.content?.[0]?.text;
			console.error(`tool ${event.toolName} failed: ${text}`);
		}
	});
}

function trimHistory() {
	const messages = agent.state.messages;
	const users = [];
	messages.forEach((message, index) => message.role === "user" && users.push(index));
	if (users.length > HISTORY_TURNS) {
		agent.state.messages = [messages[0], ...messages.slice(users[users.length - HISTORY_TURNS])];
	}
}

async function chat(message) {
	current = {
		id: message.id,
		context: message.context || "",
		thinking: !!message.thinking,
		search: message.search !== false,
		tool: message.tool || null,
	};
	agent.state.thinkingLevel = current.thinking ? "low" : "off";
	const before = [...agent.state.messages];
	let failure;
	try {
		await agent.prompt(message.text);
		const last = agent.state.messages.findLast((item) => item.role === "assistant");
		if (last?.stopReason === "error") failure = last.errorMessage;
		// Cancelled or failed turns are dropped so the next request does not continue them.
		if (last?.stopReason === "error" || last?.stopReason === "aborted") agent.state.messages = before;
	} catch (error) {
		failure = error?.message;
		agent.state.messages = before;
	} finally {
		pendingTools.clear();
	}
	trimHistory();
	send(failure === undefined
		? { t: "done", id: message.id }
		: { t: "error", id: message.id, message: describeError(failure) });
}

function note(message) {
	const now = Date.now();
	agent.state.messages = [
		...agent.state.messages,
		{ role: "user", content: message.user, timestamp: now },
		{
			role: "assistant",
			content: [{ type: "text", text: message.reply }],
			api: "openai-completions",
			provider: PROVIDER,
			model: config.model,
			usage: EMPTY_USAGE,
			stopReason: "stop",
			timestamp: now,
		},
	];
	trimHistory();
}

function handle(message) {
	switch (message.t) {
		case "config":
			setup(message);
			break;
		case "chat":
			queue = queue.then(() => chat(message));
			break;
		case "note":
			queue = queue.then(() => note(message));
			break;
		case "tool_result":
			pendingTools.get(message.call)?.(message);
			pendingTools.delete(message.call);
			break;
		case "abort":
			agent?.abort();
			break;
		case "reset":
			queue = queue.then(() => agent.reset());
			break;
	}
}

createInterface({ input: process.stdin }).on("line", (line) => {
	let message;
	try {
		message = JSON.parse(line);
	} catch {
		return;
	}
	try {
		handle(message);
	} catch (error) {
		send({ t: "error", id: message.id, message: describeError(error?.message) });
	}
}).on("close", () => process.exit(0));
