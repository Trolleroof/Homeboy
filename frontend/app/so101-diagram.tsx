"use client";

import { useEffect, useRef, useState } from "react";
import type { URDFRobot } from "urdf-loader";

const JOINT_ORDER = [
  "shoulder_pan",
  "shoulder_lift",
  "elbow_flex",
  "wrist_flex",
  "wrist_roll",
  "gripper",
] as const;

/** Mid-range display pose in URDF radians while the bridge is offline. */
const IDLE: Record<(typeof JOINT_ORDER)[number], number> = {
  shoulder_pan: 0.35,
  shoulder_lift: 0.55,
  elbow_flex: 0.85,
  wrist_flex: 0.25,
  wrist_roll: Math.PI / 2,
  gripper: 0.55,
};

const GRIPPER_CLOSED = -0.174533;
const GRIPPER_OPEN = 1.74533;

type DiagramStatus = {
  state: string;
  offline?: boolean;
  teleop?: { engaged?: boolean; ready?: boolean; record_ready?: boolean };
  estop?: { engaged?: boolean; reason?: string };
  delta?: { joints?: Record<string, { follower: number }> };
  motors?: { positions?: Record<string, number>; age_s?: number | null };
};

function jointValue(positions: Record<string, number>, name: string): number | undefined {
  const raw = positions[`${name}.pos`] ?? positions[name];
  return typeof raw === "number" ? raw : undefined;
}

function toUrdfRadians(name: string, degreesOrGripper: number): number {
  if (name === "gripper") {
    const t = Math.min(1, Math.max(0, degreesOrGripper / 100));
    return GRIPPER_CLOSED + t * (GRIPPER_OPEN - GRIPPER_CLOSED);
  }
  return degreesOrGripper * (Math.PI / 180);
}

function liveTargets(status: DiagramStatus): Record<string, number> {
  const positions: Record<string, number> = {};
  for (const name of JOINT_ORDER) {
    const fromDelta = status.delta?.joints?.[name]?.follower;
    if (typeof fromDelta === "number") positions[name] = fromDelta;
  }
  if (!Object.keys(positions).length && status.motors?.positions) {
    for (const name of JOINT_ORDER) {
      const value = jointValue(status.motors.positions, name);
      if (value !== undefined) positions[name] = value;
    }
  }
  if (status.offline || !Object.keys(positions).length) return { ...IDLE };
  const next = { ...IDLE };
  for (const name of JOINT_ORDER) {
    if (typeof positions[name] === "number") next[name] = toUrdfRadians(name, positions[name]);
  }
  // Keep the jaws turned 90° counterclockwise from the motor reading.
  if (typeof positions.wrist_roll === "number") next.wrist_roll += Math.PI / 2;
  return next;
}

function statusLabel(status: DiagramStatus): { line: string; tone: "offline" | "warn" | "live" | "rec" | "stop" } {
  if (status.offline) return { line: "Bridge offline", tone: "offline" };
  if (status.estop?.engaged || status.state === "estopped") return { line: "E-stop", tone: "stop" };
  if (status.state === "recording") return { line: "Recording", tone: "rec" };
  if (status.state === "pending") return { line: "Stopped — save or discard", tone: "warn" };
  if (status.state === "saving") return { line: "Saving…", tone: "warn" };
  if (status.teleop?.engaged && status.teleop.record_ready) return { line: "Teleop · ready to record", tone: "live" };
  if (status.teleop?.engaged) return { line: "Teleop engaged", tone: "live" };
  if (status.teleop?.ready) return { line: "Leader / follower aligned", tone: "live" };
  return { line: "Follower idle", tone: "offline" };
}

export default function So101Diagram({ status }: { status: DiagramStatus }) {
  const host = useRef<HTMLDivElement>(null);
  const statusRef = useRef(status);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);
  statusRef.current = status;

  useEffect(() => {
    const root = host.current;
    if (!root) return;
    let disposed = false;
    let frame = 0;
    let renderer: import("three").WebGLRenderer | undefined;
    let controls: { update: () => void; dispose: () => void } | undefined;
    let resize: ResizeObserver | undefined;

    const boot = async () => {
      const THREE = await import("three");
      const { OrbitControls } = await import("three/examples/jsm/controls/OrbitControls.js");
      const URDFLoader = (await import("urdf-loader")).default;
      if (disposed || !host.current) return;

      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(32, 1, 0.01, 20);
      camera.position.set(0.42, 0.22, 0.46);

      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
      renderer.setClearColor(0x000000, 0);
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.toneMapping = THREE.ACESFilmicToneMapping;
      renderer.toneMappingExposure = 1.15;
      renderer.shadowMap.enabled = true;
      root.appendChild(renderer.domElement);

      scene.add(new THREE.HemisphereLight(0xf4e7b2, 0x7d8a72, 1.15));
      const key = new THREE.DirectionalLight(0xfff6d6, 1.35);
      key.position.set(0.4, 0.7, 0.35);
      key.castShadow = true;
      key.shadow.mapSize.set(1024, 1024);
      scene.add(key);
      const fill = new THREE.DirectionalLight(0xcdd8b8, 0.55);
      fill.position.set(-0.5, 0.2, -0.2);
      scene.add(fill);

      const ground = new THREE.Mesh(
        new THREE.CircleGeometry(0.28, 48),
        new THREE.MeshStandardMaterial({ color: 0xc9cfc0, roughness: 1, metalness: 0, transparent: true, opacity: 0.45 }),
      );
      ground.rotation.x = -Math.PI / 2;
      ground.position.y = -0.002;
      ground.receiveShadow = true;
      scene.add(ground);

      const orbit = new OrbitControls(camera, renderer.domElement);
      orbit.enablePan = false;
      orbit.enableZoom = false;
      orbit.autoRotate = false;
      orbit.target.set(0, 0.1, 0);
      orbit.minPolarAngle = 0.9;
      orbit.maxPolarAngle = 1.45;
      controls = orbit;

      const fit = () => {
        const width = root.clientWidth || 420;
        const height = root.clientHeight || 260;
        camera.aspect = width / height;
        camera.updateProjectionMatrix();
        renderer!.setSize(width, height, false);
      };
      fit();
      resize = new ResizeObserver(fit);
      resize.observe(root);

      const manager = new THREE.LoadingManager();
      let meshesSettled = false;
      const meshesReady = new Promise<void>((resolve) => {
        manager.onLoad = () => { meshesSettled = true; resolve(); };
      });
      const loader = new URDFLoader(manager);
      loader.parseCollision = false;
      const localMesh = await fetch("/so101/assets/base_so101_v2.stl", { method: "HEAD" });
      loader.workingPath = localMesh.ok
        ? "/so101/"
        : "https://cdn.jsdelivr.net/gh/TheRobotStudio/SO-ARM100@main/Simulation/SO101/";
      let robot: URDFRobot;
      try {
        robot = await loader.loadAsync("/so101/so101.urdf");
        if (!meshesSettled) await Promise.race([meshesReady, new Promise((_, reject) => window.setTimeout(() => reject(new Error("mesh timeout")), 20000))]);
      } catch {
        if (!disposed) setFailed(true);
        return;
      }
      if (disposed) return;

      robot.rotation.x = -Math.PI / 2;
      if (robot.joints.wrist_roll) robot.joints.wrist_roll.ignoreLimits = true;
      for (const name of JOINT_ORDER) robot.setJointValue(name, IDLE[name]);
      let meshes = 0;
      robot.traverse((child) => {
        const mesh = child as import("three").Mesh;
        if (!mesh.isMesh) return;
        meshes += 1;
        mesh.castShadow = true;
        mesh.receiveShadow = true;
        if (mesh.geometry && !mesh.geometry.getAttribute("normal")) mesh.geometry.computeVertexNormals();
        const material = mesh.material as import("three").MeshStandardMaterial;
        if (material?.isMaterial) {
          material.roughness = 0.42;
          material.metalness = /sts|motor/i.test(mesh.name) ? 0.35 : 0.08;
        }
      });

      robot.updateMatrixWorld(true);
      const base = robot.links.base_link;
      const baseBox = new THREE.Box3();
      for (const child of base?.children ?? []) {
        const part = child as import("three").Object3D & { isURDFJoint?: boolean; isURDFCollider?: boolean };
        if (part.isURDFJoint || part.isURDFCollider) continue;
        baseBox.expandByObject(part);
      }
      if (!meshes || baseBox.isEmpty()) {
        setFailed(true);
        return;
      }
      const baseCenter = baseBox.getCenter(new THREE.Vector3());
      robot.position.x -= baseCenter.x;
      robot.position.z -= baseCenter.z;
      robot.position.y -= baseBox.min.y;
      scene.add(robot);
      robot.updateMatrixWorld(true);

      const planted = new THREE.Box3();
      for (const child of base.children) {
        const part = child as import("three").Object3D & { isURDFJoint?: boolean; isURDFCollider?: boolean };
        if (part.isURDFJoint || part.isURDFCollider) continue;
        planted.expandByObject(part);
      }
      const footprint = Math.max(planted.getSize(new THREE.Vector3()).x, planted.getSize(new THREE.Vector3()).z);
      ground.geometry.dispose();
      ground.geometry = new THREE.CircleGeometry(Math.max(footprint * 1.35, 0.08), 64);
      ground.position.y = -0.001;

      const size = new THREE.Box3().setFromObject(robot).getSize(new THREE.Vector3());
      const span = Math.max(size.x, size.y, size.z);
      const dist = span / (2 * Math.tan((camera.fov * Math.PI) / 360));
      camera.position.set(dist * 0.88, dist * 0.36, dist * 1.08);
      orbit.target.set(0, size.y * 0.34, 0);
      orbit.update();
      setReady(true);

      const current = { ...IDLE };
      const tick = () => {
        if (disposed) return;
        const target = liveTargets(statusRef.current);
        for (const name of JOINT_ORDER) {
          current[name] += (target[name] - current[name]) * 0.16;
          robot.setJointValue(name, current[name]);
        }
        orbit.update();
        renderer!.render(scene, camera);
        frame = window.requestAnimationFrame(tick);
      };
      tick();
    };

    void boot();
    return () => {
      disposed = true;
      window.cancelAnimationFrame(frame);
      resize?.disconnect();
      controls?.dispose();
      renderer?.dispose();
      renderer?.domElement.remove();
    };
  }, []);

  const label = statusLabel(status);
  const motorsFresh = status.motors?.age_s != null && status.motors.age_s < 0.6;
  const meta = failed
    ? "Model unavailable"
    : !ready
      ? "Loading official URDF…"
      : motorsFresh
        ? "Motors live"
        : status.offline
          ? "No telemetry"
          : "Motors stale";

  return (
    <div className="so101-viewer" role="img" aria-label={`SO-101 follower. ${label.line}. ${meta}.`}>
      <div className="so101-stage" ref={host} />
      <div className="so101-status">
        <p className="sketch-label">SO-101 FOLLOWER</p>
        <p className={`so101-status-line tone-${label.tone}`}>{label.line}</p>
        <p className="so101-status-meta">{meta}</p>
      </div>
    </div>
  );
}
