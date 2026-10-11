"use client";

import { useEffect, useRef, useState } from "react";

export default function FollowerView() {
  const host = useRef<HTMLDivElement>(null);
  const [modelState, setModelState] = useState("Loading…");
  const [live, setLive] = useState(false);
  useEffect(() => {
    const element = host.current;
    if (!element) return;
    let disposed = false;
    let cleanup = () => {};
    async function setup() {
      const THREE = await import("three");
      const { default: URDFLoader } = await import("urdf-loader");
      const { OrbitControls } = await import("three/examples/jsm/controls/OrbitControls.js");
      if (disposed) return;
      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.setClearColor(0x000000, 0);
      element!.appendChild(renderer.domElement);
      const scene = new THREE.Scene();
      scene.add(new THREE.HemisphereLight(0xfffdf1, 0x56644c, 2.7));
      const light = new THREE.DirectionalLight(0xffffff, 3.2);
      light.position.set(1, 2, 1); scene.add(light);
      const grid = new THREE.GridHelper(.85, 12, 0xb5c1a2, 0xd3dbc4); scene.add(grid);
      const camera = new THREE.PerspectiveCamera(36, 1, .01, 10);
      camera.position.set(.62, .42, .65);
      const controls = new OrbitControls(camera, renderer.domElement);
      controls.target.set(0, .17, 0); controls.enableDamping = true;
      controls.enablePan = false; controls.minDistance = .35; controls.maxDistance = 1.6;
      const manager = new THREE.LoadingManager();
      const loader = new URDFLoader(manager);
      loader.parseCollision = false;
      let robot: import("urdf-loader").URDFRobot | null = null;
      let modelReady = false;
      let loadFailed = false;
      let lastFresh = 0;
      let liveShown = false;
      let timer: ReturnType<typeof setTimeout> | undefined;
      let frame = 0;
      const abort = new AbortController();
      loader.fetchOptions = { signal: abort.signal };
      manager.onLoad = () => { if (!disposed && !loadFailed) { modelReady = true; setModelState(""); } };
      manager.onError = () => { loadFailed = true; if (!disposed) { modelReady = false; setModelState("Model didn't load. Reload."); } };
      const resize = new ResizeObserver(() => {
        const width = element!.clientWidth, height = element!.clientHeight;
        if (!width || !height) return;
        renderer.setSize(width, height); camera.aspect = width / height; camera.updateProjectionMatrix();
      });
      resize.observe(element!);
      function disposeScene() {
        scene.traverse(object => {
          if (object instanceof THREE.Mesh) {
            object.geometry.dispose();
            const materials = Array.isArray(object.material) ? object.material : [object.material];
            materials.forEach(material => material.dispose());
          }
        });
        grid.geometry.dispose();
        (grid.material as import("three").Material).dispose();
      }
      cleanup = () => {
        abort.abort(); clearTimeout(timer); cancelAnimationFrame(frame);
        resize.disconnect(); controls.dispose(); disposeScene(); renderer.dispose();
        renderer.domElement.remove();
      };
      loader.load("/so101/so101.urdf", loaded => {
        if (disposed) {
          loaded.traverse(object => { if (object instanceof THREE.Mesh) { object.geometry.dispose(); (Array.isArray(object.material) ? object.material : [object.material]).forEach(material => material.dispose()); } });
          return;
        }
        robot = loaded;
        // URDF uses Z-up; Three.js uses Y-up.
        robot.rotation.x = -Math.PI / 2;
        scene.add(robot);
      }, undefined, () => { if (!disposed) setModelState("Model didn't load. Reload."); });
      async function poll() {
        try {
          const response = await fetch("/api/follower", { cache: "no-store", signal: AbortSignal.any([abort.signal, AbortSignal.timeout(2000)]) });
          const pose = await response.json();
          if (!disposed && response.ok && pose.fresh && robot) {
            const names = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"];
            if (names.every(name => Number.isFinite(pose.radians?.[name])) && Number.isFinite(pose.gripper)) {
              robot.setJointValues(pose.radians);
              const joint = robot.joints.gripper;
              robot.setJointValue("gripper", joint.limit.lower + Math.min(100, Math.max(0, pose.gripper)) / 100 * (joint.limit.upper - joint.limit.lower));
              lastFresh = performance.now() - (pose.age_s ?? 0) * 1000;
            }
          }
        } catch { /* hold the last measured pose while disconnected */ }
        if (!disposed) timer = setTimeout(() => void poll(), 150);
      }
      void poll();
      function draw() {
        if (disposed) return;
        const nextLive = modelReady && lastFresh > 0 && performance.now() - lastFresh < 700;
        if (nextLive !== liveShown) { liveShown = nextLive; setLive(nextLive); }
        controls.update(); renderer.render(scene, camera); frame = requestAnimationFrame(draw);
      }
      draw();
    }
    void setup().catch(() => { if (!disposed) { cleanup(); setModelState("3D won't run here."); } });
    return () => { disposed = true; cleanup(); };
  }, []);
  return <div className="follower-view" aria-label="SO-101 follower digital twin">
    <div className="follower-canvas" ref={host} />
    <div className="follower-view-caption"><span className={`follower-live-dot ${live ? "live" : ""}`} /><strong>{live ? "Live" : "Paused"}</strong>{modelState && <span>{modelState}</span>}</div>
    <span className="follower-view-hint">Drag to spin</span>
  </div>;
}
