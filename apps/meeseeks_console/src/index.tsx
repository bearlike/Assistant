import "./index.css";
import { createRoot } from "react-dom/client";
import { App } from "./App";
const container = document.getElementById("root");
if (container) {
  createRoot(container).render(<App />);
}
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => undefined);
}
