import { LatestRuns } from "@/features/runs/RunList";
import { ResearchForm } from "@/features/research/ResearchForm";

export default function Page() {
  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold">Research</h1>
      <ResearchForm />
      <LatestRuns />
    </div>
  );
}
