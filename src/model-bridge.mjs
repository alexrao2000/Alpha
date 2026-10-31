/* CrewAI owns its conversation and ReAct tools. This process only transports the
   next model response; the CLI's own tools remain outside the experiment. */

import { CodexBackend } from './backends.mjs';

try {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const request = JSON.parse(input);
  const backend = new CodexBackend({ model: request.model, timeout: 120000 });
  const schema = { type: 'object', properties: { response: { type: 'string' } }, required: ['response'], additionalProperties: false };
  const prompt = 'Provide the next assistant response for the conversation below. ' +
    'Keep the exact response format requested by its system message, including any ReAct markers. ' +
    'Do not inspect files, run commands, browse, or use your own tools. ' +
    'If the conversation calls for a tool, express that call in its requested text format; the host framework executes it. ' +
    'Return the assistant response text as the response JSON field.\n\n' + JSON.stringify(request.messages);
  const result = await backend.complete({ schema, prompt });
  console.log(JSON.stringify({ response: result.output.response, usage: result.usage, model_calls: result.model_calls }));
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
