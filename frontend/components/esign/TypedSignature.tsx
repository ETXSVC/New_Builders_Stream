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
    if (submitting || !signerName.trim() || !signerEmail.trim()) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    renderToCanvas(signerName);
    const artifact = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!artifact) return;
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
