import { AgentWorkspace } from "./components/agent/AgentWorkspace";
import { AgentStoreProvider } from "./store/agent-store";

export default function App() {
  return <AgentStoreProvider><AgentWorkspace /></AgentStoreProvider>;
}
