/** Represents a code diff or snippet to be commented on. */
export interface CodeInput {
  filePath: string;
  content: string;
  language?: string;
}

/** A generated comment for a specific location in code. */
export interface GeneratedComment {
  filePath: string;
  line: number;
  text: string;
  severity: "info" | "suggestion" | "warning" | "error";
}

/** Provider interface for AI backends. */
export interface AIProvider {
  generateComments(input: CodeInput): Promise<GeneratedComment[]>;
}

/**
 * CommentGenerator orchestrates AI-powered comment generation.
 * It delegates to an AIProvider for the actual inference.
 */
export class CommentGenerator {
  constructor(private readonly provider: AIProvider) {}

  async analyze(inputs: CodeInput[]): Promise<GeneratedComment[]> {
    const results: GeneratedComment[] = [];

    for (const input of inputs) {
      const comments = await this.provider.generateComments(input);
      results.push(...comments);
    }

    return results;
  }
}
