import { describe, it } from "node:test";
import assert from "node:assert/strict";
import type { AIProvider, CodeInput, GeneratedComment } from "../src/ai/comment-generator.js";
import { CommentGenerator } from "../src/ai/comment-generator.js";
import { createCommentPlanNode } from "../src/ai/comment-workflow.js";
import type { WorkflowContext } from "../src/types/workflow.js";

// -- Mock Provider --

class MockProvider implements AIProvider {
  async generateComments(input: CodeInput): Promise<GeneratedComment[]> {
    return [
      {
        filePath: input.filePath,
        line: 1,
        text: `Comment for ${input.filePath}`,
        severity: "info",
      },
    ];
  }
}

// -- Tests --

describe("CommentGenerator", () => {
  it("generates comments for each input file", async () => {
    const generator = new CommentGenerator(new MockProvider());
    const inputs: CodeInput[] = [
      { filePath: "a.ts", content: "const x = 1;" },
      { filePath: "b.ts", content: "const y = 2;" },
    ];

    const comments = await generator.analyze(inputs);

    assert.equal(comments.length, 2);
    assert.equal(comments[0].filePath, "a.ts");
    assert.equal(comments[1].filePath, "b.ts");
  });
});

describe("createCommentPlanNode", () => {
  it("creates a plan node that analyzes all files", async () => {
    const provider = new MockProvider();
    const files: CodeInput[] = [
      { filePath: "x.ts", content: "code" },
      { filePath: "y.ts", content: "code" },
    ];

    const planNode = createCommentPlanNode(provider, files);
    const ctx: WorkflowContext = { data: {}, errors: [] };
    const result = await planNode.run(ctx);

    assert.equal(result.status, "completed");

    const comments = ctx.data["comments"] as GeneratedComment[];
    assert.equal(comments.length, 2);
    assert.equal(comments[0].filePath, "x.ts");
    assert.equal(comments[1].filePath, "y.ts");

    const plan = planNode.getPlan();
    assert.ok(plan);
    assert.equal(plan.steps.length, 2);
    assert.ok(plan.steps.every((s) => s.completed));
  });
});
