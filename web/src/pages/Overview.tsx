import { SetupChecklist } from "../components/SetupChecklist";
import { usePid } from "../hooks";

/** The landing page after sign-in: can you trust your evaluation, then what changed, then traffic. */
export default function Overview() {
  const pid = usePid();
  return (
    <div style={{ maxWidth: 1100 }}>
      <div className="page-head">
        <div>
          <h1>Overview</h1>
          <div className="muted small">Whether your evaluation can be trusted, how your changes are doing, and your traffic.</div>
        </div>
      </div>
      <SetupChecklist pid={pid} />
    </div>
  );
}
