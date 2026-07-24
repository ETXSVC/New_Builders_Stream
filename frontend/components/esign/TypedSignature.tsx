"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

// DocuSign-style "adopt a signature": the signer types their name, sees it
// rendered in a script font, and on submit that rendering is drawn to a
// hidden canvas and exported as a PNG blob — the exact artifact shape the
// backend's approve routes expect (multipart signature_artifact file).
export function TypedSignature({
  onSign,
  submitting,
}: {
  onSign: (args: { signerName: string; signerEmail: string; artifact: Blob }) => void;
  submitting: boolean;
}) {
  const [signerName, setSignerName] = React.useState("");
  const [signerEmail, setSignerEmail] = React.useState("");
  const canvasRef = React.useRef<HTMLCanvasElement | null>(null);
  // Synchronous double-submit guard, distinct from the `submitting` PROP
  // above: `handleSubmit` below is async and does `await
  // canvas.toBlob(...)` — a real async gap — BEFORE it ever calls
  // `onSign`, which is what actually flips the parent's `submitting`
  // state. Two clicks close together both read `submitting === false`
  // during that gap (React hasn't re-rendered with a new prop value yet,
  // because nothing has set it), so the prop-only check below was not
  // enough to stop both calls from reaching `canvas.toBlob` and,
  // eventually, both calling `onSign` — firing two concurrent approve
  // requests. A ref is set synchronously, in the same tick as the first
  // click, closing that gap; the effect further down keeps it in sync
  // with the `submitting` prop so a legitimate retry after a FAILED
  // approve (parent resets `submitting` to false) isn't permanently
  // blocked.
  const submittingRef = React.useRef(false);

  React.useEffect(() => {
    submittingRef.current = submitting;
  }, [submitting]);

  function renderToCanvas(name: string): void {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#1e293b";
    ctx.font = "40px 'Brush Script MT', cursive";
    ctx.textBaseline = "middle";
    ctx.fillText(name, 16, canvas.height / 2);
  }

  React.useEffect(() => {
    renderToCanvas(signerName || " ");
  }, [signerName]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submittingRef.current || submitting || !signerName.trim() || !signerEmail.trim()) return;
    // Set synchronously, before the first `await` below — this is what
    // actually closes the race (see submittingRef's own declaration
    // above), not the `submitting`-prop check just above, which can't
    // observe a concurrent click until React re-renders with a new prop
    // value.
    submittingRef.current = true;

    const canvas = canvasRef.current;
    if (!canvas) {
      // Never reached onSign, so the parent's `submitting` prop will
      // never flip true/false to re-sync this ref via the effect above —
      // must reset explicitly here, or every future submit attempt would
      // be silently blocked forever.
      submittingRef.current = false;
      return;
    }
    renderToCanvas(signerName);
    const artifact = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!artifact) {
      // Same reasoning as the !canvas branch above.
      submittingRef.current = false;
      return;
    }
    // From here on, onSign (synchronously, inside SigningPanel's own
    // handleApprove) is what flips the `submitting` prop true — the
    // effect above then keeps submittingRef in sync with it for the rest
    // of this request's lifecycle, including resetting back to false if
    // the approve call later fails and the parent re-enables the form.
    onSign({ signerName, signerEmail, artifact });
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="signer-name">Full name</Label>
        <Input
          id="signer-name"
          value={signerName}
          onChange={(e) => setSignerName(e.target.value)}
          disabled={submitting}
          required
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="signer-email">Email</Label>
        <Input
          id="signer-email"
          type="email"
          value={signerEmail}
          onChange={(e) => setSignerEmail(e.target.value)}
          disabled={submitting}
          required
        />
      </div>
      <div className="border border-slate-200 rounded-md p-2">
        <p className="text-xs text-slate-500 mb-1">Signature preview</p>
        <canvas ref={canvasRef} width={320} height={80} className="w-full h-20" />
      </div>
      <Button type="submit" disabled={submitting || !signerName.trim() || !signerEmail.trim()}>
        {submitting ? "Signing…" : "Approve & sign"}
      </Button>
    </form>
  );
}
