import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, type Me } from "./api";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api<Me>("/api/auth/me"), retry: false });
}

export function usePid(): string {
  const { pid } = useParams();
  return pid || "";
}

export function useRole(): "admin" | "member" | "viewer" | null {
  const pid = usePid();
  const me = useMe();
  return me.data?.projects.find((p) => p.project_id === pid)?.role ?? null;
}

export const RANGES: Record<string, number> = { "1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400 };
