import { spawn, execFileSync, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

export const root = path.resolve(process.cwd(), "..", "bridge");
export const recorderURL = "http://127.0.0.1:8621";
const store = globalThis as unknown as { willowRecorder?: { child: ChildProcess | null; log: string[]; starting: boolean } };
const state = store.willowRecorder ??= { child: null, log: [], starting: false };
export function config() {
  const arms = JSON.parse(fs.readFileSync(path.join(root, "config/arms.json"), "utf8"));
  const cameras = JSON.parse(fs.readFileSync(path.join(root, "config/cameras.json"), "utf8"));
  return { robotPort: arms.follower as string, teleopPort: arms.leader as string,
    cameras: Object.entries(cameras).map(([name, index]) => ({ name, index })) };
}
function ownedPid(): number | null {
  if (state.child && state.child.exitCode === null && state.child.signalCode === null && !state.child.killed) return state.child.pid ?? null;
  try {
    const pid = Number(fs.readFileSync(path.join(root, "logs/recorder.pid"), "utf8"));
    if (!Number.isInteger(pid) || pid < 1) return null;
    const command = execFileSync("/bin/ps", ["-p", String(pid), "-o", "command="], { encoding: "utf8" });
    return command.includes(path.join(root, "scripts/record_server.py")) && command.includes("--port 8621") ? pid : null;
  } catch { return null; }
}
export function status() {
  const pid = ownedPid();
  return { running: pid !== null, starting: state.starting, pid, log: state.log.slice(-30) };
}
export async function start(task: string) {
  if (state.starting) return { ok: true, ...status() };
  if (status().running) {
    const current = await fetch(`${recorderURL}/status`, { signal: AbortSignal.timeout(2000) }).then(r => r.json());
    const settings = config();
    if (!current.offline_bridge || ![settings.robotPort, settings.teleopPort].every(p => fs.existsSync(p))) return { ok: true, ...status() };
    await stop();
    const deadline = Date.now() + 8000;
    while (status().running && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 100));
    if (status().running) throw new Error("Offline bridge is still shutting down; retry connection shortly.");
  }
  state.starting = true;
  try {
    const settings = config();
    const offline = ![settings.robotPort, settings.teleopPort].every(p => fs.existsSync(p));
    const processes = execFileSync("/bin/ps", ["-Ao", "pid=,command="], { encoding: "utf8" });
    if (!offline && processes.split("\n").some(line => /python[^ ]* .*\/health_server\.py(?: |$)/.test(line))) {
      throw new Error("A robot health poller is running. Stop the SUDS dashboard and health_server.py before starting Homeboy; they also open the arm ports.");
    }
    if (!task.trim() || task.length > 200 || /[\r\n]/.test(task)) throw new Error("Use a task label of 1–200 characters on one line.");
    if (settings.robotPort === settings.teleopPort) throw new Error("Leader and follower require different ports.");
    for (const port of [settings.robotPort, settings.teleopPort]) {
      if (!/^\/dev\/tty\.[\w.-]+$/.test(port)) throw new Error(`Invalid arm port: ${port}`);
      if (offline) continue;
      let owners = "";
      try { owners = execFileSync("/usr/sbin/lsof", ["-t", port], { encoding: "utf8", timeout: 3000 }); }
      catch (error) { if ((error as { status?: number }).status !== 1) throw error; }
      if (owners.trim()) throw new Error(`Arm port ${port} is occupied by process ${owners.trim()}. Stop its controller first.`);
    }
    try { const r = await fetch(`${recorderURL}/status`, { signal: AbortSignal.timeout(500) }); if (r.ok) throw new Error("Another recorder already owns port 8621."); }
    catch (error) { if ((error as Error).message.includes("already owns")) throw error; }
    const python = process.env.WILLOW_PYTHON ?? "/Users/nikhi/SUDS/.venv/bin/python";
    const args = [path.join(root, "scripts/record_server.py"), "--port", "8621", "--repo-id", "willow/speedhacks-wrist-v1", "--task", task,
      "--robot-port", settings.robotPort, "--teleop-port", settings.teleopPort, "--fps", "30", "--sync-seconds", "2", "--auto-estop", "--commit-seconds", "120"];
    for (const camera of settings.cameras) args.push("--camera", `${camera.name}=${camera.index}`);
    if (offline) args.push("--offline");
    fs.mkdirSync(path.join(root, "logs"), { recursive: true });
    state.log = [];
    const child = spawn(python, args, { cwd: root, env: { ...process.env, PYTHONUNBUFFERED: "1" }, stdio: ["pipe", "pipe", "pipe"] });
    state.child = child;
    if (child.pid) fs.writeFileSync(path.join(root, "logs/recorder.pid"), String(child.pid));
    const append = (data: Buffer | string) => {
      const text = data.toString();
      fs.appendFileSync(path.join(root, "logs/recorder.log"), text);
      state.log.push(...text.split("\n").filter(Boolean));
      state.log = state.log.slice(-100);
    };
    child.stdout?.on("data", append); child.stderr?.on("data", append);
    child.on("error", error => append(error.message));
    child.on("exit", (code, signal) => append(`Recorder exited: ${code ?? signal}\n`));
    child.stdin?.end("\n");
    return { ok: true, ...status() };
  } finally { state.starting = false; }
}
export async function stop() {
  const pid = ownedPid();
  if (!pid) return { ok: true, ...status() };
  process.kill(pid, "SIGINT");
  return { ok: true, stopping: true };
}
