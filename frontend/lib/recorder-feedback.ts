type Status = {
  offline?: boolean;
  state: string;
  message?: string;
  teleop?: { ready?: boolean; engaged?: boolean; record_ready?: boolean; worst?: number };
  data_quality?: { ready?: boolean; issues?: string[] };
};

export function teleopReady(status: Status): boolean {
  // During live tracking, use the controller's readiness, not an instantaneous
  // leader/follower pose gap while the operator is moving the leader.
  return !status.offline && (status.teleop?.engaged
    ? status.teleop.record_ready === true : status.teleop?.ready === true);
}

export function recorderWaitError(status: Status | undefined, action: string): string {
  if (!status || status.offline) return `The recorder is unavailable while ${action}. Check bridge status.`;
  if (status.message?.startsWith("frame error:") || status.message?.startsWith("save failed:")) {
    return `The arms are connected, but the recording could not be saved: ${status.message}. Stop the take and restart the bridge before recording again.`;
  }
  if (status.data_quality?.issues?.length) return `Could not finish ${action}: ${status.data_quality.issues.join(" · ")}`;
  if (!teleopReady(status)) {
    return `The bridge is connected, but teleop is not ready while ${action}. ${status.teleop?.engaged
      ? "Check the live joint readings." : "Use Sync arms before recording."}`;
  }
  return `The bridge is connected, but ${action} did not finish (recorder: ${status.state}). You can still stop the take.`;
}
