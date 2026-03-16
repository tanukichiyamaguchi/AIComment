import type { NodeResult, WorkflowContext } from "../types/workflow.js";
import type { WorkflowNode } from "./node.js";

export interface WorkflowResult {
  success: boolean;
  context: WorkflowContext;
  nodeResults: Map<string, NodeResult>;
}

/**
 * Orchestration engine that runs a sequence of workflow nodes.
 * Nodes execute in order; if a node fails, the workflow stops.
 */
export class WorkflowEngine {
  private readonly nodes: WorkflowNode[] = [];

  addNode(node: WorkflowNode): this {
    this.nodes.push(node);
    return this;
  }

  /** Execute all nodes in sequence. */
  async run(initialData?: Record<string, unknown>): Promise<WorkflowResult> {
    const ctx: WorkflowContext = {
      data: initialData ?? {},
      errors: [],
    };

    const nodeResults = new Map<string, NodeResult>();

    for (const node of this.nodes) {
      const result = await node.run(ctx);
      nodeResults.set(node.config.id, result);

      if (result.status === "failed") {
        return { success: false, context: ctx, nodeResults };
      }
    }

    return { success: true, context: ctx, nodeResults };
  }
}
