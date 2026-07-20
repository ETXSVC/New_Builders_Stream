"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

export interface LeadFormValues {
  contact_name: string;
  project_name: string;
  email: string;
  phone: string;
  project_type: string;
  estimated_value: string;
  notes: string;
}

export const EMPTY_LEAD_FORM: LeadFormValues = {
  contact_name: "",
  project_name: "",
  email: "",
  phone: "",
  project_type: "",
  estimated_value: "",
  notes: "",
};

// Serializes form values into the backend's request shape: empty optional
// strings become null rather than "" (the backend's validators reject empty
// strings on length-floored fields).
export function leadPayload(values: LeadFormValues) {
  return {
    contact_name: values.contact_name,
    project_name: values.project_name,
    email: values.email,
    phone: values.phone || null,
    project_type: values.project_type,
    estimated_value: values.estimated_value || null,
    notes: values.notes || null,
  };
}

export function LeadForm({
  initial,
  submitLabel,
  onSubmit,
  submitting,
  error,
}: {
  initial: LeadFormValues;
  submitLabel: string;
  onSubmit: (values: LeadFormValues) => void;
  submitting: boolean;
  error: string | null;
}) {
  const [values, setValues] = React.useState<LeadFormValues>(initial);

  function set<K extends keyof LeadFormValues>(key: K) {
    return (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
      setValues((v) => ({ ...v, [key]: e.target.value }));
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(values);
      }}
      className="flex flex-col gap-4 w-full max-w-md"
    >
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="contact_name">Contact name</Label>
        <Input id="contact_name" value={values.contact_name} onChange={set("contact_name")} disabled={submitting} required minLength={2} />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="project_name">Project name</Label>
        <Input id="project_name" value={values.project_name} onChange={set("project_name")} disabled={submitting} required />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="email">Email</Label>
        <Input id="email" type="email" value={values.email} onChange={set("email")} disabled={submitting} required />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="phone">Phone (optional)</Label>
        <Input id="phone" value={values.phone} onChange={set("phone")} disabled={submitting} maxLength={20} />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="project_type">Project type</Label>
        <Input id="project_type" value={values.project_type} onChange={set("project_type")} disabled={submitting} required placeholder="Remodel, new build, addition…" />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="estimated_value">Estimated value (optional)</Label>
        <Input id="estimated_value" type="number" min="0" step="0.01" value={values.estimated_value} onChange={set("estimated_value")} disabled={submitting} />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="notes">Notes (optional)</Label>
        <Textarea id="notes" value={values.notes} onChange={set("notes")} disabled={submitting} />
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <Button type="submit" disabled={submitting}>
        {submitLabel}
      </Button>
    </form>
  );
}
