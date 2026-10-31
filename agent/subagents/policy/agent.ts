import { defineAgent } from "eve";

export default defineAgent({
  description: "Alpha support policy: produce the structured result for this workflow stage.",
  model: "openai/gpt-6-luna",
  tool: false,
});
