"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { DataTable } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import { api } from "@/utils/api";
import { API_KEY_ENDPOINTS } from "@/utils/api-endpoints";
import { toast } from "@/components/ui/use-toast";
import { Plus, Copy, Check, Trash2 } from "lucide-react";
import { CopyToClipboard } from "react-copy-to-clipboard";
import { format } from "date-fns";
import { getErrorMessage } from "@/lib/error-message";
import { useApiQuery } from "@/hooks/use-api-query";
import { ApiKey, ApiKeyCreateResponse } from "@/types/api";

export default function ApiKeysPage() {
  const [createOpen, setCreateOpen] = useState(false);
  const [newLabel, setNewLabel] = useState("");
  const [newKey, setNewKey] = useState("");
  const [copied, setCopied] = useState(false);
  const [keyToRevoke, setKeyToRevoke] = useState<ApiKey | null>(null);

  const {
    data: keys = [],
    isLoading,
    refetch,
  } = useApiQuery<ApiKey[]>(
    async () => {
      const res = await api.get<ApiKey[]>(API_KEY_ENDPOINTS.BASE);
      return res.data ?? [];
    },
    { errorToast: "加载 API 密钥失败", initialData: [] },
  );

  const handleCreate = async () => {
    try {
      const res = await api.post<ApiKeyCreateResponse>(API_KEY_ENDPOINTS.BASE, {
        label: newLabel,
      });
      setNewKey(res.data.key);
      void refetch();
    } catch (error) {
      toast({
        title: "创建密钥失败",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    }
  };

  const handleRevoke = async () => {
    if (!keyToRevoke) return;
    try {
      await api.delete(API_KEY_ENDPOINTS.BY_ID(keyToRevoke.id));
      toast({ title: "API 密钥已吊销", variant: "success" });
      setKeyToRevoke(null);
      void refetch();
    } catch (error) {
      toast({
        title: "吊销密钥失败",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    }
  };

  const handleDialogClose = (open: boolean) => {
    if (!open) {
      setNewKey("");
      setNewLabel("");
      setCopied(false);
    }
    setCreateOpen(open);
  };

  const columns = [
    { key: "label" as keyof ApiKey, label: "标签", width: 150 },
    {
      key: "key_prefix" as keyof ApiKey,
      label: "密钥",
      width: 120,
      render: (value: string) => (
        <code className="text-xs font-mono">{value}...</code>
      ),
    },
    {
      key: "created_at" as keyof ApiKey,
      label: "创建时间",
      width: 120,
      render: (value: string) => format(new Date(value), "yyyy-MM-dd"),
    },
    {
      key: "last_used_at" as keyof ApiKey,
      label: "最近使用",
      width: 120,
      render: (value: string | null) =>
        value ? format(new Date(value), "yyyy-MM-dd") : "从未使用",
    },
    {
      key: "id" as keyof ApiKey,
      label: "",
      width: 40,
      render: (_: string, row: ApiKey) => (
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setKeyToRevoke(row)}
          className="size-7"
        >
          <Trash2 className="size-3.5 text-onSurface-danger-primary" />
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold font-fustat">API 密钥</h1>
        <Dialog open={createOpen} onOpenChange={handleDialogClose}>
          <DialogTrigger asChild>
            <Button size="sm">
              <Plus className="size-4 mr-1" /> 创建密钥
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>创建 API 密钥</DialogTitle>
            </DialogHeader>
            {!newKey ? (
              <div className="space-y-4 mt-2">
                <div className="space-y-2">
                  <Label htmlFor="api-key-label">标签</Label>
                  <Input
                    id="api-key-label"
                    value={newLabel}
                    onChange={(e) => setNewLabel(e.target.value)}
                    placeholder="例如：生产环境"
                  />
                </div>
                <Button
                  onClick={handleCreate}
                  disabled={!newLabel}
                  className="w-full"
                >
                  创建
                </Button>
              </div>
            ) : (
              <div className="space-y-4 mt-2">
                <div className="space-y-2">
                  <Label htmlFor="api-key-new">您的 API 密钥</Label>
                  <div className="flex gap-2">
                    <Input
                      id="api-key-new"
                      value={newKey}
                      readOnly
                      className="font-mono text-sm"
                    />
                    <CopyToClipboard
                      text={newKey}
                      onCopy={() => {
                        setCopied(true);
                        setTimeout(() => setCopied(false), 2000);
                      }}
                    >
                      <Button variant="outline" size="icon">
                        {copied ? (
                          <Check className="size-4" />
                        ) : (
                          <Copy className="size-4" />
                        )}
                      </Button>
                    </CopyToClipboard>
                  </div>
                  <p className="text-xs text-onSurface-danger-primary">
                    请保存此密钥，关闭后将无法再次查看。
                  </p>
                </div>
                <Button
                  onClick={() => handleDialogClose(false)}
                  className="w-full"
                >
                  完成
                </Button>
              </div>
            )}
          </DialogContent>
        </Dialog>
      </div>

      {isLoading ? (
        <TableSkeleton rows={3} columns={4} />
      ) : keys.length === 0 ? (
        <EmptyState
          title="暂无 API 密钥"
          description="创建您的第一个 API 密钥，开始使用 Agentar 记忆平台 API。"
        />
      ) : (
        <Card className="border-memBorder-primary overflow-hidden">
          <DataTable
            data={keys}
            columns={columns}
            getRowKey={(row) => row.id}
          />
        </Card>
      )}

      <DeleteConfirmationModal
        isOpen={!!keyToRevoke}
        onClose={() => setKeyToRevoke(null)}
        onConfirm={handleRevoke}
        title="吊销 API 密钥"
        description="使用该密钥的应用将立即失效，此操作无法撤销。"
        itemName={keyToRevoke?.label ?? ""}
        confirmButtonText="吊销"
      />
    </div>
  );
}
