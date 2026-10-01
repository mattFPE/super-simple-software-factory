# The Console extends the visualizer, and still never writes the trace

The visualizer was built read-only — agents → sqlite → web ui, one archive flag as its only write. We are growing it into the Console, which launches and stops Runs, rather than building a sibling app, so launching and watching share one server and one URL. The invariant survives in its essential form: the Console never writes `sssf.db`. A launch spawns an ADW process whose tracer writes the db exactly as a terminal launch would, and the UI sees the Run through its existing poll; GitHub writes (claiming an issue) are the ADW's, not the Console's. Rules that already live in Python — which issues are Runnable, how to stop a Run safely — are called from the server, never reimplemented in TypeScript.

## Considered Options

- **A separate console app** linking into the visualizer: keeps the visualizer pure, but doubles the servers, ports and Vue scaffolding for what is one engineer's one workspace.
