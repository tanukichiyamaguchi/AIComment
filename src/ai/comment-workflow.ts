import type { PlanStep, NodeConfig, NodeResult, WorkflowContext } from "../types/workflow.js";
import { WorkflowNode } from "../workflow/node.js";
import { PlanNode } from "../workflow/plan-node.js";
import type { AIProvider, CodeInput, GeneratedComment } from "./comment-generator.js";
import { CommentGenerator } from "./comment-generator.js";

/**
 * A workflow node that runs AI comment generation for a single file.
 */
class AnalyzeFileNode extends WorkflowNode {
  private readonly generator: CommentGenerator;
  private readonly input: CodeInput;

  constructor(config: NodeConfig, generator: CommentGenerator, input: CodeInput) {
    super(config);
    this.generator = generator;
    this.input = input;
  }

  protected async execute(ctx: WorkflowContext): Promise<NodeResult> {
    const comments = await this.generator.analyze([this.input]);
    const existing = (ctx.data["comments"] as GeneratedComment[] | undefined) ?? [];
    ctx.data["comments"] = [...existing, ...comments];
    return { status: "completed", output: comments };
  }
}

/**
 * Creates a PlanNode that plans and executes AI comment generation
 * across multiple files using the workflow orchestration engine.
 */
export function createCommentPlanNode(
  provider: AIProvider,
  files: CodeInput[],
): PlanNode {
  const generator = new CommentGenerator(provider);

  const generateSteps = (): PlanStep[] =>
    files.map((file, i) => ({
      id: `analyze-${i}`,
      description: `Analyze ${file.filePath}`,
      completed: false,
    }));

  const childFactory = (step: PlanStep, _ctx: WorkflowContext): WorkflowNode => {
    const index = parseInt(step.id.replace("analyze-", ""), 10);
    const file = files[index];
    return new AnalyzeFileNode(
      { id: step.id, name: step.description },
      generator,
      file,
    );
  };

  return new PlanNode(
    { id: "comment-plan", name: "AI Comment Plan" },
    generateSteps,
    childFactory,
  );
}
