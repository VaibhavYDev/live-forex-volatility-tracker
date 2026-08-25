import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "../ErrorBoundary";

let quiet: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  // React logs caught errors to console.error by design; the noise would bury
  // real failures in the run output.
  quiet = vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => quiet.mockRestore());

function Boom({ throws }: { throws: boolean }): JSX.Element {
  if (throws) throw new Error("series disposed");
  return <p>chart</p>;
}

function Recoverable() {
  const [broken, setBroken] = useState(true);
  return (
    <>
      <button type="button" onClick={() => setBroken(false)}>
        repair
      </button>
      <ErrorBoundary region="Price chart">
        <Boom throws={broken} />
      </ErrorBoundary>
    </>
  );
}

describe("ErrorBoundary", () => {
  it("renders its children when nothing is wrong", () => {
    render(
      <ErrorBoundary region="Price chart">
        <Boom throws={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("chart")).toBeInTheDocument();
  });

  it("contains a throw instead of blanking the page", () => {
    render(
      <>
        <p>status banner</p>
        <ErrorBoundary region="Price chart">
          <Boom throws />
        </ErrorBoundary>
      </>,
    );
    // The whole point: the component that tells you something is wrong must
    // survive something going wrong.
    expect(screen.getByText("status banner")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/price chart stopped rendering/i);
  });

  it("names the region so the user knows which pane died", () => {
    render(
      <ErrorBoundary region="Volatility pane">
        <Boom throws />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Volatility pane");
  });

  it("says the feed is unaffected, because it is", () => {
    render(
      <ErrorBoundary region="Price chart">
        <Boom throws />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/feed is still connected/i);
  });

  it("recovers when the underlying problem is fixed", async () => {
    const user = userEvent.setup();
    render(<Recoverable />);
    expect(screen.getByRole("alert")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "repair" }));
    await user.click(screen.getByRole("button", { name: /try again/i }));

    expect(screen.getByText("chart")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("re-catches if the retry fails again", async () => {
    const user = userEvent.setup();
    render(
      <ErrorBoundary region="Price chart">
        <Boom throws />
      </ErrorBoundary>,
    );
    await user.click(screen.getByRole("button", { name: /try again/i }));
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("passes axe in its failed state", async () => {
    const { container } = render(
      <ErrorBoundary region="Price chart">
        <Boom throws />
      </ErrorBoundary>,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it("logs the failure for whoever has the console open", () => {
    render(
      <ErrorBoundary region="Price chart">
        <Boom throws />
      </ErrorBoundary>,
    );
    expect(quiet).toHaveBeenCalledWith(
      "[Price chart]",
      expect.objectContaining({ message: "series disposed" }),
      expect.anything(),
    );
  });
});
