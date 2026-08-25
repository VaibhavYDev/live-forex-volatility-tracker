import { vi } from "vitest";

/**
 * A recording 2D context.
 *
 * jsdom has no canvas, so `getContext("2d")` returns null and every draw path in
 * the app silently no-ops under test. That would leave the z-pane's segmentation
 * loop — the only genuinely tricky code in it, since it has to break the line
 * across regime changes and across minutes we could not measure — completely
 * uncovered. Recording the calls instead lets the tests assert the *shape* of
 * what was drawn without pulling in a canvas implementation.
 */

export interface Op {
  readonly op: string;
  readonly args: readonly number[];
  readonly stroke: string;
}

export interface Recorder {
  readonly ops: Op[];
  /** One entry per `beginPath`…`stroke` pair, in order. */
  paths(): { stroke: string; points: [number, number][] }[];
}

export function stubCanvas(): Recorder {
  const ops: Op[] = [];

  function make(): CanvasRenderingContext2D {
    const ctx = {
      strokeStyle: "",
      fillStyle: "",
      lineWidth: 1,
      lineJoin: "miter",
      lineCap: "butt",
    } as unknown as CanvasRenderingContext2D;

    const record =
      (op: string) =>
      (...args: number[]) => {
        ops.push({ op, args, stroke: String(ctx.strokeStyle) });
      };

    return Object.assign(ctx, {
      setTransform: record("setTransform"),
      clearRect: record("clearRect"),
      beginPath: record("beginPath"),
      moveTo: record("moveTo"),
      lineTo: record("lineTo"),
      stroke: record("stroke"),
      arc: record("arc"),
      fill: record("fill"),
      closePath: record("closePath"),
    });
  }

  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    () => make() as unknown as RenderingContext,
  );

  return {
    ops,
    paths() {
      const out: { stroke: string; points: [number, number][] }[] = [];
      let current: { stroke: string; points: [number, number][] } | null = null;
      for (const { op, args, stroke } of ops) {
        if (op === "beginPath") current = { stroke, points: [] };
        else if (op === "moveTo" || op === "lineTo") {
          current?.points.push([args[0]!, args[1]!]);
          if (current) current.stroke = stroke;
        } else if (op === "stroke" && current) {
          out.push(current);
          current = null;
        }
      }
      return out;
    },
  };
}

/** jsdom reports every element as 0x0, so a width-driven renderer never runs. */
export function stubWidth(px: number): void {
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(px);
}
