import { useParams } from 'react-router-dom';
import { useRunStream } from '../hooks/useRunStream.js';
import { RunTimeline, SourceList, ActivityFeed } from '../components/studio.jsx';
export default function Run() {
  const { id } = useParams();
  const events = useRunStream(id);
  return (
    <div className="max-w-4xl mx-auto p-6 grid grid-cols-2 gap-6">
      <div><h2 className="font-display text-2xl">Collecting data</h2><RunTimeline events={events} /><SourceList events={events} /></div>
      <div><ActivityFeed events={events} /></div>
    </div>
  );
}
