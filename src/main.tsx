
  import { createRoot } from "react-dom/client";
  import App from "./App.tsx";
  import "./index.css";
  import { DOCUMENT_TITLE } from "./lib/product";

  document.title = DOCUMENT_TITLE;

  createRoot(document.getElementById("root")!).render(<App />);
  