import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatDate } from "@/lib/format";

export interface ClientProjectShape {
  id: string;
  name: string;
  status: string;
  site_address: string;
  projected_start_date: string | null;
  phase_count: number;
  task_count: number;
  completed_task_count: number;
}

export function ClientProjectDashboard({ project }: { project: ClientProjectShape }) {
  return (
    <Card className="max-w-md">
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>{project.name}</CardTitle>
          <StatusBadge status={project.status} />
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm text-slate-600">
        <p>{project.site_address || "Site address pending"}</p>
        <p>Projected start: {formatDate(project.projected_start_date)}</p>
        <p>
          {project.phase_count} {project.phase_count === 1 ? "phase" : "phases"} ·{" "}
          {project.completed_task_count} of {project.task_count} tasks complete
        </p>
      </CardContent>
    </Card>
  );
}
