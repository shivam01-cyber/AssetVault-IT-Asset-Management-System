import { useEffect } from "react";

const APP_URL = `${process.env.REACT_APP_BACKEND_URL}/api/`;

// The application itself is a server-rendered Flask + Jinja2 app served under /api.
// This shell simply forwards visitors to it.
export default function App() {
  useEffect(() => {
    window.location.replace(APP_URL);
  }, []);

  return (
    <div
      data-testid="redirect-shell"
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "#0b1120",
        color: "#e2e8f0",
        fontFamily: "system-ui, sans-serif",
      }}
    >
      <a data-testid="open-app-link" href={APP_URL} style={{ color: "#38bdf8" }}>
        Opening AssetVault&hellip;
      </a>
    </div>
  );
}
