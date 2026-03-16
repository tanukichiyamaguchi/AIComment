// Workflow orchestration
export { WorkflowEngine } from "./workflow/engine.js";
export { WorkflowNode } from "./workflow/node.js";
export { PlanNode } from "./workflow/plan-node.js";

// AI comment generation
export { CommentGenerator } from "./ai/comment-generator.js";
export { createCommentPlanNode } from "./ai/comment-workflow.js";

// Types
export type {
  NodeConfig,
  NodeResult,
  NodeStatus,
  Plan,
  PlanStep,
  WorkflowContext,
} from "./types/workflow.js";
export type {
  AIProvider,
  CodeInput,
  GeneratedComment,
} from "./ai/comment-generator.js";
