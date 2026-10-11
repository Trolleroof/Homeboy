import { spawn, execFileSync, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { armLease } from "./arm-lease";
import { config, root, recorderURL, status as recorderStatus, stop } from "./robot-bridge";

export type Role = "leader" | "follower";
export type CalibrationStatus = {
  role: Role | null; running: boolean;
  phase: "idle" | "starting" | "home" | "range" | "saving" | "done" | "failed";
  joints: { name: string; min: number; pos: number; max: number }[];
  canFinish: boolean; command: string; savedTo: string; error: string; log: string[];
};
type Session = CalibrationStatus & { child: ChildProcess | null; buffer: string };
const store = globalThis as unknown as { homeboyCalibration?: Session };
const state = store.homeboyCalibration ??= { role: null, running: false, phase: "idle", joints: [], canFinish: false, command: "", savedTo: "", error: "", log: [], child: null, buffer: "" };
const JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "gripper"];
export function status(): CalibrationStatus {
  return { role: state.role, running: state.running, phase: state.phase, joints: state.joints,
    canFinish: state.phase === "range" && JOINTS.every(name => state.joints.some(joint => joint.name === name && joint.max - joint.min >= 800)),
    command: state.command, savedTo: state.savedTo, error: state.error, log: state.log.slice(-30) };
}
function consume(chunk: string) {
  state.buffer += chunk.replace(/\u001b\[[0-9;]*[A-Za-z]/g, "");
  if (/type 'c' and press ENTER to run calibration/i.test(state.buffer)) {
    state.buffer = ""; state.child?.stdin?.write("c\n"); return;
  }
  if (/middle of its range of motion and press ENTER/i.test(state.buffer)) {
    state.buffer = ""; state.phase = "home"; return;
  }
  const lines = state.buffer.split("\n"); state.buffer = lines.pop() ?? "";
  for (const raw of lines) {
    const line = raw.trim();
    const row = /^([a-z][a-z0-9_]*)\s*\|\s*(-?\d+)\s*\|\s*(-?\d+)\s*\|\s*(-?\d+)\s*$/.exec(line);
    if (row) {
      if (state.phase !== "saving") state.phase = "range";
      const joint = { name: row[1], min: Number(row[2]), pos: Number(row[3]), max: Number(row[4]) };
      state.joints = [...state.joints.filter(item => item.name !== joint.name), joint];
    } else if (line && !/^NAME\s*\||^-{5,}$/.test(line)) {
      state.log = [...state.log.slice(-59), line];
      if (/Calibration saved to/.test(line)) state.savedTo = line.replace(/^.*Calibration saved to\s*/, "");
    }
  }
}
export async function start(role: Role) {
  if (armLease.held) throw new Error("A calibration is already running.");
  armLease.held = true;
  try {
    if (recorderStatus().starting) throw new Error("Wait for bridge startup to finish.");
    if (recorderStatus().running) {
      const response = await fetch(`${recorderURL}/status`, { cache: "no-store", signal: AbortSignal.timeout(2000) });
      if (!response.ok) throw new Error("Cannot check the bridge. Stop it before calibration.");
      const current = await response.json();
      if (!["idle", "estopped"].includes(current.state) || current.teleop?.engaged || current.autonomy?.active || current.calibration) throw new Error("Stop teaching or playback and save/discard the take first.");
      await stop();
      const deadline = Date.now() + 8000;
      while (recorderStatus().running && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 100));
      if (recorderStatus().running) throw new Error("Bridge is still stopping; try again shortly.");
    }
    const settings = config();
    const port = role === "leader" ? settings.teleopPort : settings.robotPort;
    if (!/^\/dev\/tty\.[\w.-]+$/.test(port) || !fs.existsSync(port)) throw new Error(`${role} is not plugged in: ${port}`);
    let owners = "";
    try { owners = execFileSync("/usr/sbin/lsof", ["-t", port], { encoding: "utf8", timeout: 3000 }); }
    catch (error) { if ((error as { status?: number }).status !== 1) throw error; }
    if (owners.trim()) throw new Error(`Another controller owns this arm port (PID ${owners.trim()}). Stop it first.`);
    const bin = process.env.HOMEBOY_CALIBRATE_BIN ?? path.join(path.dirname(process.env.WILLOW_PYTHON ?? "/Users/nikhi/SUDS/.venv/bin/python"), "lerobot-calibrate");
    const prefix = role === "leader" ? "teleop" : "robot";
    const args = [`--${prefix}.type=so101_${role}`, `--${prefix}.port=${port}`, `--${prefix}.id=${role}`];
    Object.assign(state, { role, running: true, phase: "starting", joints: [], savedTo: "", error: "", log: [], buffer: "", command: `${bin} ${args.join(" ")}` });
    const child = spawn(bin, args, { cwd: root, env: { ...process.env, PYTHONUNBUFFERED: "1" }, stdio: ["pipe", "pipe", "pipe"] });
    state.child = child;
    child.stdout?.on("data", (data: Buffer) => consume(data.toString()));
    child.stderr?.on("data", (data: Buffer) => { state.log = [...state.log, ...data.toString().split("\n").filter(Boolean)].slice(-60); });
    child.stdin?.on("error", error => { state.error = error.message; });
    child.on("error", error => { state.error = error.message; state.phase = "failed"; });
    child.on("close", (code, signal) => {
      if (state.buffer.trim()) consume("\n");
      state.running = false; state.child = null; armLease.held = false;
      if (code === 0 && state.savedTo) state.phase = "done";
      else { state.phase = "failed"; state.error ||= signal ? "Calibration cancelled. Run calibration again before using this arm if home was changed." : state.log.filter(line => /Error:|Exception:/.test(line)).at(-1) ?? `LeRobot exited (${code}). Open the output for details.`; }
    });
    return status();
  } catch (error) { armLease.held = false; throw error; }
}
export function advance() {
  if (!state.running || !state.child?.stdin?.writable) throw new Error("No calibration prompt is waiting.");
  if (state.phase === "home") { state.phase = "range"; state.child.stdin.write("\n"); }
  else if (status().canFinish) { state.phase = "saving"; state.child.stdin.write("\n"); }
  else throw new Error("Sweep every joint through its full range before saving.");
  return status();
}
export async function cancel() {
  if (!state.child || !state.running) throw new Error("No calibration is running.");
  const child = state.child;
  const closed = new Promise<void>(resolve => child.once("close", () => resolve()));
  child.kill("SIGINT");
  const timer = setTimeout(() => { if (state.running) child.kill("SIGKILL"); }, 8000);
  await closed; clearTimeout(timer);
  return status();
}
