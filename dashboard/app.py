"""
StreamNet Web Dashboard and Interactive Experiment Interface.

Provides a modern web-based graphical interface for:
  - Triggering live TCP vs UDP benchmarks with custom network degradation sliders
  - Inspecting master experiment telemetry and comparison charts
  - Visualizing publication-grade graphs
"""

from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from experiments.runner import ExperimentRunner
from experiments.scenarios import ExperimentScenario
from experiments.visualizer import generate_all_figures, load_master_dataframe
from network.conditions import NetworkCondition
from network.profiles import PROFILES

BASE_DIR = Path(__file__).resolve().parent.parent
RESULTS_DIR = BASE_DIR / "experiments" / "results"
PLOTS_DIR = RESULTS_DIR / "plots"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

RESULTS_DIR.mkdir(parents=True, exist_ok=True)
PLOTS_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="StreamNet Evaluation Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/plots", StaticFiles(directory=str(PLOTS_DIR)), name="plots")


class RunRequest(BaseModel):
    scenario_name: str = "Custom Live Run"
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_percent: float = 0.0
    bandwidth_mbps: Optional[float] = None
    total_frames: int = 90
    fps: int = 30
    jpeg_quality: int = 70


@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_file = TEMPLATES_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>StreamNet Dashboard - Template loading error</h1>", status_code=500)
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


@app.get("/api/profiles")
async def get_profiles():
    """Return all available predefined network profiles."""
    return {
        key: {
            "name": cond.name,
            "latency_ms": cond.latency_ms,
            "jitter_ms": cond.jitter_ms,
            "loss_percent": cond.loss_percent,
            "bandwidth_mbps": cond.bandwidth_mbps,
        }
        for key, cond in PROFILES.items()
    }


@app.get("/api/experiments")
async def get_experiments():
    """Return all historical experiment records from master CSV."""
    csv_file = RESULTS_DIR / "master_experiment_results.csv"
    if not csv_file.exists():
        return []
    df = load_master_dataframe(csv_file)
    return df.to_dict(orient="records")


@app.post("/api/run-scenario")
async def run_live_scenario(req: RunRequest):
    """Execute a live paired TCP vs UDP experiment with the requested degradation parameters."""
    condition = NetworkCondition(
        name=req.scenario_name,
        latency_ms=req.latency_ms,
        jitter_ms=req.jitter_ms,
        loss_percent=req.loss_percent,
        bandwidth_mbps=req.bandwidth_mbps if (req.bandwidth_mbps and req.bandwidth_mbps > 0) else None,
    )

    scenario = ExperimentScenario(
        scenario_id=f"LIVE_{int(req.latency_ms)}D_{int(req.loss_percent)}L",
        name=req.scenario_name,
        category="live_dashboard",
        condition=condition,
        total_frames=req.total_frames,
        fps=req.fps,
        jpeg_quality=req.jpeg_quality,
    )

    runner = ExperimentRunner(results_dir=RESULTS_DIR)
    record = runner.run_scenario(scenario)

    # Regenerate publication figures in background
    try:
        generate_all_figures(output_dir=PLOTS_DIR)
    except Exception:
        pass

    return record


@app.post("/api/generate-plots")
async def generate_plots():
    """Trigger plot generation from master CSV."""
    csv_file = RESULTS_DIR / "master_experiment_results.csv"
    if not csv_file.exists():
        raise HTTPException(status_code=404, detail="No experiment data found.")
    plots = generate_all_figures(output_dir=PLOTS_DIR)
    return {"generated_plots": [p.name for p in plots]}


@app.get("/api/plots")
async def list_plots():
    """List available plot image filenames."""
    plot_files = list(PLOTS_DIR.glob("*.png"))
    return [p.name for p in sorted(plot_files)]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
