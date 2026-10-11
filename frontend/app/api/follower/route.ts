import { NextResponse } from "next/server";
import { recorderURL } from "@/lib/robot-bridge";

// Reads the existing recorder cache; never opens a serial port or writes motors.
export async function GET() {
  try {
    const response = await fetch(`${recorderURL}/motors`, { cache: "no-store", signal: AbortSignal.timeout(1500) });
    if (!response.ok) throw new Error("Follower telemetry is unavailable.");
    const pose = await response.json();
    const fresh = pose.hardware_available === true && Object.keys(pose.radians ?? {}).length === 5 && Number.isFinite(pose.gripper);
    return NextResponse.json({ fresh, age_s: pose.age_s, radians: pose.radians, gripper: pose.gripper, source: "follower measured positions", units: "radians" }, { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    return NextResponse.json({ fresh: false, radians: {}, message: (error as Error).message }, { headers: { "Cache-Control": "no-store" } });
  }
}
