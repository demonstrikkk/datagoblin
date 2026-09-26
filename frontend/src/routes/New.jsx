import { useState } from 'react';
import { PromptComposer, PlanPreview } from '../components/studio.jsx';
import { postJSON, unwrap } from '../lib/api.js';
import { useNavigate } from 'react-router-dom';
export default function New() {
  const [plan, setPlan] = useState(null);
  const nav = useNavigate();
  return (
    <div className="max-w-3xl mx-auto p-6">
      <PromptComposer onPlan={setPlan} />
      <PlanPreview plan={plan} onRun={async (p) => {
        const r = unwrap(await postJSON('/api/runs', { plan_id: p.plan_id }));
        nav(`/runs/${r.run_id}`);
      }} />
    </div>
  );
}
