import { describe, it, mock } from "node:test";
import assert from "node:assert/strict";
import type { NodeConfig, NodeResult, PlanStep, WorkflowContext } from "../src/types/workflow.js";
import { WorkflowNode } from "../src/workflow/node.js";
import { PlanNode } from "../src/workflow/plan-node.js";
import { WorkflowEngine } from "../src/workflow/engine.js";

// -- Helpers --

class StubNode extends WorkflowNode {
  private readonly result: NodeResult;

  constructor(id: string, result: NodeResult) {
    super({ id, name: id });
    this.result = result;
  }

  protected async execute(_ctx: WorkflowContext): Promise<NodeResult> {
    return this.result;
  }
}

class AccumulatorNode extends WorkflowNode {
  constructor(id: string) {
    super({ id, name: id });
  }

  protected async execute(ctx: WorkflowContext): Promise<NodeResult> {
    const count = (ctx.data["count"] as number | undefined) ?? 0;
    ctx.data["count"] = count + 1;
    return { status: "completed" };
  }
}

// -- Tests --

describe("WorkflowEngine", () => {
  it("runs nodes in sequence and returns success", async () => {
    const engine = new WorkflowEngine();
    engine
      .addNode(new StubNode("a", { status: "completed" }))
      .addNode(new StubNode("b", { status: "completed" }));

    const result = await engine.run();

    assert.equal(result.success, true);
    assert.equal(result.nodeResults.size, 2);
  });

  it("stops on first failure", async () => {
    const engine = new WorkflowEngine();
    engine
      .addNode(new StubNode("a", { status: "completed" }))
      .addNode(new StubNode("b", { status: "failed", error: "boom" }))
      .addNode(new StubNode("c", { status: "completed" }));

    const result = await engine.run();

    assert.equal(result.success, false);
    assert.equal(result.nodeResults.size, 2); // c never ran
    assert.equal(result.nodeResults.get("b")?.error, "boom");
  });

  it("passes context data between nodes", async () => {
    const engine = new WorkflowEngine();
    engine.addNode(new AccumulatorNode("a")).addNode(new AccumulatorNode("b"));

    const result = await engine.run();

    assert.equal(result.success, true);
    assert.equal(result.context.data["count"], 2);
  });

  it("accepts initial data", async () => {
    const engine = new WorkflowEngine();
    engine.addNode(new AccumulatorNode("a"));

    const result = await engine.run({ count: 10 });

    assert.equal(result.context.data["count"], 11);
  });
});

describe("WorkflowNode", () => {
  it("catches thrown errors and returns failed status", async () => {
    class ThrowingNode extends WorkflowNode {
      constructor() {
        super({ id: "throw", name: "throw" });
      }
      protected async execute(): Promise<NodeResult> {
        throw new Error("unexpected");
      }
    }

    const node = new ThrowingNode();
    const ctx: WorkflowContext = { data: {}, errors: [] };
    const result = await node.run(ctx);

    assert.equal(result.status, "failed");
    assert.equal(result.error, "unexpected");
    assert.equal(node.status, "failed");
  });
});

describe("PlanNode", () => {
  it("generates a plan and executes all steps", async () => {
    const steps: PlanStep[] = [
      { id: "s1", description: "Step 1", completed: false },
      { id: "s2", description: "Step 2", completed: false },
    ];

    const planNode = new PlanNode(
      { id: "plan", name: "Test Plan" },
      () => steps.map((s) => ({ ...s })),
      (step) => new StubNode(step.id, { status: "completed" }),
    );

    const ctx: WorkflowContext = { data: {}, errors: [] };
    const result = await planNode.run(ctx);

    assert.equal(result.status, "completed");
    const plan = planNode.getPlan();
    assert.ok(plan);
    assert.equal(plan.steps.length, 2);
    assert.ok(plan.steps.every((s) => s.completed));
  });

  it("re-plans on failure and retries", async () => {
    let callCount = 0;

    const planNode = new PlanNode(
      { id: "plan", name: "Retry Plan" },
      () => [{ id: "s1", description: "Step 1", completed: false }],
      () => {
        callCount++;
        // First call fails, second succeeds
        if (callCount === 1) {
          return new StubNode("s1", { status: "failed", error: "transient" });
        }
        return new StubNode("s1", { status: "completed" });
      },
      1, // maxRetries = 1
    );

    const ctx: WorkflowContext = { data: {}, errors: [] };
    const result = await planNode.run(ctx);

    assert.equal(result.status, "completed");
    assert.equal(callCount, 2);
  });

  it("fails after exhausting retries", async () => {
    const planNode = new PlanNode(
      { id: "plan", name: "Fail Plan" },
      () => [{ id: "s1", description: "Step 1", completed: false }],
      () => new StubNode("s1", { status: "failed", error: "persistent" }),
      1,
    );

    const ctx: WorkflowContext = { data: {}, errors: [] };
    const result = await planNode.run(ctx);

    assert.equal(result.status, "failed");
    assert.ok(result.error?.includes("exhausted"));
  });
});
