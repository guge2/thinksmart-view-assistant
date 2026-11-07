import { createInterface } from "node:readline";
import { Agent } from "@earendil-works/pi-agent-core";
import { streamSimple } from "@earendil-works/pi-ai/api/openai-completions";
import { streamSimple as streamResponses } from "@earendil-works/pi-ai/api/openai-responses";

const HISTORY_TURNS = 6;
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
let current = { id: 0, context: "", thinking: false, search: true, tool: null, expert: false };
let queue = Promise.resolve();
const pendingTools = new Map();
let lastTurn;

function send(message) {
	process.stdout.write(`${JSON.stringify(message)}\n`);
}

function describeError(message) {
	let text = String(message || "unknown error");
	for (const key of [config?.apiKey, config?.expert?.apiKey]) {
		if (key) text = text.split(key).join("[redacted]");
	}
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

function profile() {
	return current.expert && config.expert?.apiKey ? config.expert : config;
}

function modelFor(target) {
	return {
		id: target.model,
		name: target.model,
		api: target.api || "openai-completions",
		provider: target.provider || "bailian",
		baseUrl: target.baseUrl,
		input: ["text"],
		reasoning: true,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: target.contextWindow || 131072,
		maxTokens: target.maxTokens,
		compat: target.api === "openai-responses" ? {} : {
			thinkingFormat: target.provider === "openrouter" ? "openrouter" : "qwen",
			supportsDeveloperRole: false,
			supportsStore: false,
			supportsReasoningEffort: false,
			maxTokensField: "max_tokens",
		},
	};
}

function setup(message) {
	config = message;
	const model = modelFor(message);
	const tools = message.tools.map(({ function: spec }) => ({
		name: spec.name,
		label: spec.name,
		description: spec.description,
		parameters: spec.parameters,
		execute: (toolCallId, args, signal) => callTool(toolCallId, spec.name, args, signal),
	}));
	agent = new Agent({
		initialState: { systemPrompt: message.system, model, thinkingLevel: "off", tools },
		streamFn: (target, context, options) => {
			const selected = profile();
			const responses = target.api === "openai-responses";
			return (responses ? streamResponses : streamSimple)(target, context, {
				...options,
				maxTokens: selected.maxTokens,
				...(responses ? {} : { temperature: config.temperature }),
				timeoutMs: responses ? 240000 : 60000,
				maxRetries: 1,
			});
		},
		getApiKey: () => profile().apiKey,
		transformContext: async (messages) => {
			const [head, ...rest] = messages;
			if (head?.role !== "system" || !current.context) return messages;
			return [{ ...head, content: `${config.system}${current.context}` }, ...rest];
		},
		onPayload: (params) => {
			const payload = { ...params };
			const selected = profile();
			delete payload.thinking_budget;
			if (selected.api === "openai-responses") {
				payload.reasoning = { ...payload.reasoning, effort: current.expert ? (selected.reasoningEffort || "high") : "medium" };
				if (config.search && current.search) {
					payload.tools = [...(payload.tools || []), { type: "web_search" }];
				}
				if (current.tool && payload.input?.at(-1)?.role === "user") {
					payload.tool_choice = { type: "function", name: current.tool };
				}
			} else if (selected.provider === "openrouter") {
				payload.reasoning = { effort: current.thinking ? "low" : "minimal" };
				if (config.providerOrder?.length) payload.provider = { order: config.providerOrder };
				if (config.search && current.search) {
					payload.tools = [...(payload.tools || []), {
						// Gemini's native search currently times out inside streamed OpenRouter requests.
						type: "openrouter:web_search", parameters: { engine: "exa", mode: "fast" },
					}];
				}
			} else {
				payload.enable_thinking = current.thinking;
				if (current.thinking) payload.thinking_budget = config.thinkingBudget;
				if (config.search && current.search) {
					payload.enable_search = true;
					payload.search_options = { search_strategy: "turbo" };
				}
			}
			// Only the first request of a turn is forced; after the tool result the model must be free to answer.
			if (current.tool && payload.messages?.at(-1)?.role === "user") {
				payload.tool_choice = { type: "function", function: { name: current.tool } };
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
		const system = messages[0]?.role === "system" ? [messages[0]] : [];
		agent.state.messages = [...system, ...messages.slice(users[users.length - HISTORY_TURNS])];
	}
}

async function chat(message) {
	current = {
		id: message.id,
		context: message.context || "",
		thinking: !!message.thinking,
		search: message.search !== false,
		tool: message.tool || null,
		expert: !!message.expert && !!config.expert?.apiKey,
	};
	agent.state.model = modelFor(profile());
	agent.state.thinkingLevel = current.expert ? (config.expert.reasoningEffort || "high") : current.thinking ? "low" : "off";
	const before = [...agent.state.messages];
	lastTurn = { id: message.id, before, messages: [] };
	let failure;
	try {
		await agent.prompt(message.text);
		const last = agent.state.messages.findLast((item) => item.role === "assistant");
		if (last?.stopReason === "error") failure = last.errorMessage;
	} catch (error) {
		failure = error?.message;
	} finally {
		lastTurn.messages = agent.state.messages.slice(before.length);
		pendingTools.clear();
	}
	send(failure === undefined
		? { t: "done", id: message.id }
		: { t: "error", id: message.id, message: describeError(failure) });
}

function heard(message) {
	if (lastTurn?.id !== message.id) return;
	// Keep completed tool calls even when their spoken acknowledgement was cut:
	// setting an alarm or starting music has already happened on the device.
	const results = new Set(lastTurn.messages.filter((m) => m.role === "toolResult").map((m) => m.toolCallId));
	const turn = [];
	for (const item of lastTurn.messages) {
		if (item.role === "user" || item.role === "toolResult") turn.push(item);
		else if (item.role === "assistant") {
			const content = item.content.filter((block) => block.type === "toolCall" && results.has(block.id));
			if (content.length) turn.push({ ...item, content, stopReason: "toolUse" });
		}
	}
	agent.state.messages = [...lastTurn.before];
	if (message.text || results.size) {
		agent.state.messages.push(...turn);
		const text = message.text
			? message.text + (message.interrupted ? "（播报被用户打断，只听到了以上内容。）" : "")
			: "（工具已执行，用户在播报前打断了回复。）";
		appendReply(text);
	}
	lastTurn = undefined;
	trimHistory();
}

function appendReply(text) {
	const selected = profile();
	agent.state.messages.push({
		role: "assistant", content: [{ type: "text", text }],
		api: selected.api || "openai-completions", provider: selected.provider || "bailian", model: selected.model,
		usage: EMPTY_USAGE, stopReason: "stop", timestamp: Date.now(),
	});
}

function note(message) {
	const now = Date.now();
	agent.state.messages = [
		...agent.state.messages,
		{ role: "user", content: message.user, timestamp: now },
		{
			role: "assistant",
			content: [{ type: "text", text: message.reply }],
			api: config.api || "openai-completions",
			provider: config.provider || "bailian",
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
		case "models":
			queue = queue.then(() => { config = message; });
			break;
		case "chat":
			queue = queue.then(() => chat(message));
			break;
		case "note":
			queue = queue.then(() => note(message));
			break;
		case "heard":
			queue = queue.then(() => heard(message));
			break;
		case "tool_result":
			pendingTools.get(message.call)?.(message);
			pendingTools.delete(message.call);
			break;
		case "abort":
			if (message.id === current.id) agent?.abort();
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
