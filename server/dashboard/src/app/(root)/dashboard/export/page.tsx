"use client";

import { useState } from "react";
import { Download } from "lucide-react";
import axios from "axios";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "@/components/ui/use-toast";
import { getErrorMessage } from "@/lib/error-message";
import { cn } from "@/lib/utils";
import { api } from "@/utils/api";
import { CATEGORY_ENDPOINTS, EXPORT_ENDPOINTS } from "@/utils/api-endpoints";
import { useApiQuery } from "@/hooks/use-api-query";
import { Category, CategoryListResponse } from "@/types/api";

// Radix Select 的 value 不允许空字符串，用哨兵值表示「全部分类」。
const CATEGORY_ALL = "__all__";

type ExportFormat = "json" | "csv";

function parseFilename(disposition: unknown): string {
  if (typeof disposition !== "string" || !disposition) return "";
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch {
      // fall through to plain filename
    }
  }
  const match = disposition.match(/filename="?([^";]+)"?/i);
  return match ? match[1] : "";
}

export default function ExportPage() {
  const [format, setFormat] = useState<ExportFormat>("json");
  const [userId, setUserId] = useState("");
  const [agentId, setAgentId] = useState("");
  const [runId, setRunId] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [category, setCategory] = useState("");
  const [exporting, setExporting] = useState(false);

  const { data: categories = [] } = useApiQuery<Category[]>(
    async () => {
      const res = await api.get<CategoryListResponse>(CATEGORY_ENDPOINTS.BASE);
      return Array.isArray(res.data?.categories) ? res.data.categories : [];
    },
    { initialData: [] },
  );

  const handleExport = async () => {
    const params = new URLSearchParams();
    params.set("format", format);
    const filters: Record<string, string> = {
      user_id: userId.trim(),
      agent_id: agentId.trim(),
      run_id: runId.trim(),
      tenant_id: tenantId.trim(),
      session_id: sessionId.trim(),
      category: category,
    };
    for (const [key, value] of Object.entries(filters)) {
      if (value) params.set(key, value);
    }

    setExporting(true);
    try {
      const res = await api.get(
        `${EXPORT_ENDPOINTS.BASE}?${params.toString()}`,
        { responseType: "blob" },
      );
      const blob = res.data as Blob;
      const filename =
        parseFilename(res.headers?.["content-disposition"]) ||
        `agentar-memories.${format}`;
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
      toast({
        title: "导出成功",
        description: `已开始下载 ${filename}`,
        variant: "success",
      });
    } catch (error) {
      let message = getErrorMessage(error);
      if (axios.isAxiosError(error) && error.response?.data instanceof Blob) {
        try {
          const parsed = JSON.parse(await error.response.data.text());
          const detail = parsed?.detail ?? parsed?.error;
          if (detail) message = String(detail);
        } catch {
          // keep default message
        }
      }
      toast({
        title: "导出失败",
        description: message,
        variant: "destructive",
      });
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold font-fustat">导出</h1>

      <Card className="border-memBorder-primary">
        <CardContent className="p-6 space-y-4">
          <div className="space-y-2">
            <Label>导出格式</Label>
            <div className="flex gap-2">
              {(["json", "csv"] as const).map((fmt) => (
                <Button
                  key={fmt}
                  variant="outline"
                  size="sm"
                  aria-pressed={format === fmt}
                  onClick={() => setFormat(fmt)}
                  className={cn(
                    format === fmt &&
                      "border-memPurple-300 bg-surface-default-secondary",
                  )}
                >
                  {fmt.toUpperCase()}
                </Button>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="space-y-2">
              <Label htmlFor="export-user-id">用户 ID (user_id)</Label>
              <Input
                id="export-user-id"
                placeholder="可选"
                value={userId}
                onChange={(e) => setUserId(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="export-agent-id">智能体 ID (agent_id)</Label>
              <Input
                id="export-agent-id"
                placeholder="可选"
                value={agentId}
                onChange={(e) => setAgentId(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="export-run-id">运行 ID (run_id)</Label>
              <Input
                id="export-run-id"
                placeholder="可选"
                value={runId}
                onChange={(e) => setRunId(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="export-tenant-id">租户 ID (tenant_id)</Label>
              <Input
                id="export-tenant-id"
                placeholder="可选"
                value={tenantId}
                onChange={(e) => setTenantId(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="export-session-id">会话 ID (session_id)</Label>
              <Input
                id="export-session-id"
                placeholder="可选"
                value={sessionId}
                onChange={(e) => setSessionId(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>分类 (category)</Label>
              <Select
                value={category || CATEGORY_ALL}
                onValueChange={(value) =>
                  setCategory(value === CATEGORY_ALL ? "" : value)
                }
              >
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="全部分类" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={CATEGORY_ALL}>全部分类</SelectItem>
                  {categories.map((c) => (
                    <SelectItem key={c.name} value={c.name}>
                      {c.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <p className="text-xs text-onSurface-default-tertiary">
            导出在服务端遍历全量记忆，数据量大时请耐心等待。筛选条件均为可选，全部留空时导出全部记忆。
          </p>

          <Button onClick={handleExport} disabled={exporting}>
            <Download className="size-4 mr-1" />
            {exporting ? "导出中..." : "导出下载"}
          </Button>
        </CardContent>
      </Card>

      <Card className="border-memBorder-primary">
        <CardContent className="p-6 space-y-3">
          <p className="text-sm font-medium">字段说明</p>
          <div className="text-xs text-onSurface-default-secondary space-y-2">
            <p>
              {
                "JSON：顶层包含 meta（exported_at、filters、total）与 memories 数组；每条记忆包含 id、memory、categories、user_id、agent_id、run_id、tenant_id、session_id、created_at、updated_at。"
              }
            </p>
            <p>
              {
                "CSV：列依次为 id、memory、categories、user_id、agent_id、run_id、tenant_id、session_id、created_at、updated_at；多个分类以「;」连接；文件带 UTF-8 BOM 头，可直接用 Excel 打开。"
              }
            </p>
            <p>导出需要管理员权限。</p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
