import type { NodeConfig, NodeResult, NodeStatus, WorkflowContext } from "../types/workflow.js";

/**
 * Abstract base class for all workflow nodes.
 * Each node receives a shared context, performs work, and returns a result.
 */
export abstract class WorkflowNode {
  readonly config: NodeConfig;
  private _status: NodeStatus = "pending";

  constructor(config: NodeConfig) {
    this.config = config;
  }

  get status(): NodeStatus {
    return this._status;
  }

  protected setStatus(status: NodeStatus): void {
    this._status = status;
  }

  /** Execute this node within the given workflow context. */
  async run(ctx: WorkflowContext): Promise<NodeResult> {
    this.setStatus("running");
    try {
      const result = await this.execute(ctx);
      this.setStatus(result.status);
      return result;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.setStatus("failed");
      return { status: "failed", error: message };
    }
  }

  /** Subclasses implement their logic here. */
  protected abstract execute(ctx: WorkflowContext): Promise<NodeResult>;
}
