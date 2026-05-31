import { STATUS_LABEL } from "@/lib/api";

const FAILED = new Set(["failed_no_data", "failed_validation", "failed_crashed"]);

export function StatusBadge({ status }: { status: string }) {
  const dotClass = FAILED.has(status) ? "failed_no_data" : status;
  return (
    <span className={`badge ${status}`}>
      <span className={`dot bdot ${dotClass}`} />
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}
