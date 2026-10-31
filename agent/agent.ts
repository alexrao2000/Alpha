import { defineAgent } from "eve";

export default defineAgent({
  description: "Alpha support fleet and workflow experiments.",
  model: "openai/gpt-6-luna",
  tool: false,
});
