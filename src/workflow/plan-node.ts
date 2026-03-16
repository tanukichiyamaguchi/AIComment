import type { NodeConfig, NodeResult, Plan, PlanStep, WorkflowContext } from "../types/workflow.js";
import { WorkflowNode } from "./node.js";

export type PlanGenerator = (ctx: WorkflowContext) => Promise<PlanStep[]> | PlanStep[];

/**
 * PlanNode enters planning mode before downstream execution.
 *
 * It generates a plan (list of steps), stores it on the context, then
 * sequentially executes child nodes that correspond to each plan step.
 * If any step fails, the plan node re-plans from the point of failure.
 */
export class PlanNode extends WorkflowNode {
  private readonly generateSteps: PlanGenerator;
  private readonly childFactory: (step: PlanStep, ctx: WorkflowContext) => WorkflowNode;
  private plan: Plan | null = null;
  private readonly maxRetries: number;

  constructor(
    config: NodeConfig,
    generateSteps: PlanGenerator,
    childFactory: (step: PlanStep, ctx: WorkflowContext) => WorkflowNode,
    maxRetries = 1,
  ) {
    super(config);
    this.generateSteps = generateSteps;
    this.childFactory = childFactory;
    this.maxRetries = maxRetries;
  }

  /** Returns the current plan, if one has been generated. */
  getPlan(): Plan | null {
    return this.plan;
  }

  protected async execute(ctx: WorkflowContext): Promise<NodeResult> {
    this.setStatus("planning");

    const steps = await this.generateSteps(ctx);
    this.plan = { steps, createdAt: new Date() };

    ctx.data[`${this.config.id}:plan`] = this.plan;

    let attempt = 0;

    while (attempt <= this.maxRetries) {
      const pendingSteps = this.plan.steps.filter((s) => !s.completed);

      if (pendingSteps.length === 0) {
        return { status: "completed", output: this.plan };
      }

      let failed = false;

      for (const step of pendingSteps) {
        const child = this.childFactory(step, ctx);
        const result = await child.run(ctx);

        if (result.status === "completed") {
          step.completed = true;
        } else {
          ctx.errors.push(
            `PlanNode(${this.config.id}): step "${step.id}" failed — ${result.error ?? "unknown error"}`,
          );
          failed = true;
          break;
        }
      }

      if (!failed) {
        return { status: "completed", output: this.plan };
      }

      attempt++;

      if (attempt <= this.maxRetries) {
        // Re-plan: regenerate remaining steps from current context
        const newSteps = await this.generateSteps(ctx);
        // Keep completed steps, replace pending ones
        const completedIds = new Set(this.plan.steps.filter((s) => s.completed).map((s) => s.id));
        this.plan.steps = [
          ...this.plan.steps.filter((s) => s.completed),
          ...newSteps.filter((s) => !completedIds.has(s.id)),
        ];
        this.plan.createdAt = new Date();
        ctx.data[`${this.config.id}:plan`] = this.plan;
      }
    }

    return {
      status: "failed",
      error: `PlanNode(${this.config.id}): exhausted ${this.maxRetries} retries`,
    };
  }
}
