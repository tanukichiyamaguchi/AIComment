/** Status of a workflow node execution. */
export type NodeStatus = "pending" | "planning" | "running" | "completed" | "failed";

/** Base context passed through the workflow. */
export interface WorkflowContext {
  /** Arbitrary key-value data shared between nodes. */
  data: Record<string, unknown>;
  /** Accumulated errors from node executions. */
  errors: string[];
}

/** Result returned by a node after execution. */
export interface NodeResult {
  status: "completed" | "failed";
  output?: unknown;
  error?: string;
}

/** A plan produced by the PlanNode before execution. */
export interface Plan {
  steps: PlanStep[];
  createdAt: Date;
}

export interface PlanStep {
  id: string;
  description: string;
  completed: boolean;
}

/** Configuration for a workflow node. */
export interface NodeConfig {
  id: string;
  name: string;
  /** Maximum execution time in ms. 0 = no limit. */
  timeoutMs?: number;
}
