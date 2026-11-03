import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";
import { EnvironmentChart, isEnvironmentChartSpec, type EnvironmentChartSpec } from "./EnvironmentChart";

interface Props {
  content: string;
  streaming?: boolean;
}

function normalizeMarkdown(content: string): string {
  const lines = content.split("\n");
  const output: string[] = [];
  for (const line of lines) {
    const isTableRow = /^\s*\|/.test(line);
    const previous = output.at(-1);
    if (
      isTableRow
      && previous
      && previous.trim()
      && !/^\s*\|/.test(previous)
    ) {
      output.push("");
    }
    if (
      !isTableRow
      && previous
      && /^\s*\|/.test(previous)
      && line.trim()
    ) {
      output.push("");
    }
    output.push(line);
  }
  return output.join("\n");
}

type MessagePart =
  | { type: "markdown"; content: string }
  | { type: "chart"; spec: EnvironmentChartSpec };

function splitChartBlocks(content: string): MessagePart[] {
  const parts: MessagePart[] = [];
  const pattern = /```chart\s*\n([\s\S]*?)\n```/g;
  let cursor = 0;
  for (const match of content.matchAll(pattern)) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(match[1]);
    } catch {
      continue;
    }
    if (!isEnvironmentChartSpec(parsed)) continue;
    if (match.index > cursor) {
      parts.push({ type: "markdown", content: content.slice(cursor, match.index) });
    }
    parts.push({ type: "chart", spec: parsed });
    cursor = match.index + match[0].length;
  }
  if (cursor < content.length || !parts.length) {
    parts.push({ type: "markdown", content: content.slice(cursor) });
  }
  return parts;
}

function MarkdownPart({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeKatex]}
      components={{
        a: ({ node: _node, ...props }) => (
          <a {...props} target="_blank" rel="noreferrer" />
        ),
        table: ({ children }) => (
          <div className="message-table-wrap">
            <table>{children}</table>
          </div>
        ),
      }}
    >
      {normalizeMarkdown(content)}
    </ReactMarkdown>
  );
}

export function MessageBody({ content, streaming = false }: Props) {
  const parts = splitChartBlocks(content);
  return (
    <div className={`message-markdown${streaming ? " is-streaming" : ""}`}>
      {parts.map((part, index) => (
        part.type === "chart"
          ? <EnvironmentChart key={`chart-${index}`} spec={part.spec} />
          : <MarkdownPart key={`markdown-${index}`} content={part.content} />
      ))}
    </div>
  );
}
