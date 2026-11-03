import type { CandidateWindow, WindowVersion } from "@/store/types";

export function getVersionMap(versions: WindowVersion[]) {
  return Object.fromEntries(versions.map((version) => [version.id, version]));
}

export function getVisibleCandidates(candidates: CandidateWindow[]) {
  return candidates.filter((candidate) => !candidate.isDeleted);
}
