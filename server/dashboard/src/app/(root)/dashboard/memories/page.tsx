"use client";

import { useEffect, useRef, useState } from "react";
import { Trash2 } from "lucide-react";
import { format } from "date-fns";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { DataTable } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { toast } from "@/components/ui/use-toast";
import { getErrorMessage } from "@/lib/error-message";
import { api } from "@/utils/api";
import { CATEGORY_ENDPOINTS, MEMORY_ENDPOINTS } from "@/utils/api-endpoints";
import { useApiQuery } from "@/hooks/use-api-query";
import { Category, CategoryListResponse, Memory } from "@/types/api";

// Radix Select 的 value 不允许空字符串，用哨兵值表示「全部分类」。
const CATEGORY_ALL = "__all__";

const PAGE_SIZE = 20;
// Keep in sync with ALL_MEMORIES_LIMIT in server/main.py.
const MEMORY_FETCH_LIMIT = 1000;

export default function MemoriesPage() {
  const [userId, setUserId] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [selectedMemory, setSelectedMemory] = useState<Memory | null>(null);
  const [memoryToDelete, setMemoryToDelete] = useState<Memory | null>(null);
  const [page, setPage] = useState(0);
  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "";

  const {
    data: memories = [],
    isLoading,
    refetch,
  } = useApiQuery<Memory[]>(
    async () => {
      const params: Record<string, string | number> = {
        top_k: MEMORY_FETCH_LIMIT,
      };
      const trimmedUserId = userId.trim();
      const trimmedTenantId = tenantId.trim();
      const trimmedSessionId = sessionId.trim();
      if (trimmedUserId) params.user_id = trimmedUserId;
      if (trimmedTenantId) params.tenant_id = trimmedTenantId;
      if (trimmedSessionId) params.session_id = trimmedSessionId;
      if (categoryFilter) params.category = categoryFilter;
      const res = await api.get(MEMORY_ENDPOINTS.BASE, { params });
      const raw = res.data?.results ?? res.data ?? [];
      return Array.isArray(raw) ? raw : [];
    },
    { errorToast: "加载记忆失败", initialData: [] },
  );

  const { data: categories = [] } = useApiQuery<Category[]>(
    async () => {
      const res = await api.get<CategoryListResponse>(CATEGORY_ENDPOINTS.BASE);
      return Array.isArray(res.data?.categories) ? res.data.categories : [];
    },
    { initialData: [] },
  );

  // 分类筛选变化时重新加载（跳过首次渲染，避免与 useApiQuery 的初始请求重复）。
  const categoryFilterInitialized = useRef(false);
  useEffect(() => {
    if (!categoryFilterInitialized.current) {
      categoryFilterInitialized.current = true;
      return;
    }
    setPage(0);
    void refetch();
  }, [categoryFilter, refetch]);

  const totalPages = Math.ceil(memories.length / PAGE_SIZE);
  const paginatedMemories = memories.slice(
    page * PAGE_SIZE,
    (page + 1) * PAGE_SIZE,
  );

  const handleDelete = async () => {
    if (!memoryToDelete) return;
    try {
      await api.delete(MEMORY_ENDPOINTS.BY_ID(memoryToDelete.id));
      toast({ title: "记忆已删除", variant: "success" });
      if (selectedMemory?.id === memoryToDelete.id) setSelectedMemory(null);
      setMemoryToDelete(null);
      void refetch();
    } catch (error) {
      toast({
        title: "删除记忆失败",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    }
  };

  const columns = [
    {
      key: "memory" as keyof Memory,
      label: "内容",
      width: 400,
      render: (value: string) => (
        <span className="line-clamp-2 text-sm">{value}</span>
      ),
    },
    { key: "user_id" as keyof Memory, label: "用户", width: 100 },
    { key: "agent_id" as keyof Memory, label: "智能体", width: 100 },
    {
      key: "tenant_id" as keyof Memory,
      label: "租户",
      width: 90,
      render: (value: string | null) => value ?? "--",
    },
    {
      key: "session_id" as keyof Memory,
      label: "会话",
      width: 90,
      render: (value: string | null) => value ?? "--",
    },
    {
      key: "created_at" as keyof Memory,
      label: "创建时间",
      width: 120,
      render: (value: string) =>
        value ? format(new Date(value), "yyyy-MM-dd") : "--",
    },
    {
      key: "metadata" as keyof Memory,
      label: "分类",
      width: 140,
      render: (_value: Memory[keyof Memory], row: Memory) => {
        const categories = row.metadata?.categories ?? [];
        if (categories.length === 0) {
          return <span className="text-onSurface-default-tertiary">--</span>;
        }
        return (
          <div className="flex flex-wrap gap-1">
            {categories.slice(0, 3).map((category) => (
              <Badge
                key={category}
                variant="secondary"
                className="px-1.5 py-0 text-xs font-normal"
              >
                {category}
              </Badge>
            ))}
            {categories.length > 3 && (
              <Badge
                variant="outline"
                className="px-1.5 py-0 text-xs font-normal"
              >
                +{categories.length - 3}
              </Badge>
            )}
          </div>
        );
      },
    },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold font-fustat">记忆</h1>

      <div className="flex flex-wrap gap-3">
        <Input
          placeholder="按用户 ID (user_id) 筛选（可选）"
          value={userId}
          onChange={(e) => setUserId(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              setPage(0);
              refetch();
            }
          }}
          className="w-64"
        />
        <Input
          placeholder="按租户 ID (tenant_id) 筛选（可选）"
          value={tenantId}
          onChange={(e) => setTenantId(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              setPage(0);
              refetch();
            }
          }}
          className="w-64"
        />
        <Input
          placeholder="按会话 ID (session_id) 筛选（可选）"
          value={sessionId}
          onChange={(e) => setSessionId(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              setPage(0);
              refetch();
            }
          }}
          className="w-64"
        />
        <Select
          value={categoryFilter || CATEGORY_ALL}
          onValueChange={(value) =>
            setCategoryFilter(value === CATEGORY_ALL ? "" : value)
          }
        >
          <SelectTrigger className="w-64">
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

      {isLoading ? (
        <TableSkeleton rows={5} columns={4} />
      ) : memories.length === 0 ? (
        <EmptyState
          title="暂无记忆"
          description="发送 POST /memories 请求即可创建第一条记忆。"
        >
          <pre className="text-xs text-left bg-surface-default-secondary p-3 rounded font-mono overflow-x-auto mt-3 max-w-lg">
            {`curl -X POST ${apiUrl}/memories \\
  -H "X-API-Key: <你的密钥>" \\
  -H "Content-Type: application/json" \\
  -d '{"messages": [{"role": "user", "content": "我喜欢徒步"}], "user_id": "alice"}'`}
          </pre>
        </EmptyState>
      ) : (
        <>
          <Card className="border-memBorder-primary overflow-hidden">
            <DataTable
              data={paginatedMemories}
              columns={columns}
              getRowKey={(row) => row.id}
              onRowClick={(row) => setSelectedMemory(row)}
              getRowClassName={(row) =>
                selectedMemory?.id === row.id
                  ? "bg-surface-default-tertiary"
                  : undefined
              }
            />
          </Card>
          {totalPages > 1 && (
            <div className="flex items-center justify-between text-sm text-onSurface-default-tertiary">
              <span>
                第 {page * PAGE_SIZE + 1}–
                {Math.min((page + 1) * PAGE_SIZE, memories.length)} 条，共{" "}
                {memories.length} 条
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page === 0}
                  onClick={() => setPage((p) => p - 1)}
                >
                  上一页
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page >= totalPages - 1}
                  onClick={() => setPage((p) => p + 1)}
                >
                  下一页
                </Button>
              </div>
            </div>
          )}
        </>
      )}

      <Sheet
        open={!!selectedMemory}
        onOpenChange={(open) => {
          if (!open) setSelectedMemory(null);
        }}
      >
        <SheetContent className="sm:max-w-md">
          <SheetHeader>
            <SheetTitle>记忆详情</SheetTitle>
            <SheetDescription className="sr-only">
              查看记忆内容与元数据
            </SheetDescription>
          </SheetHeader>
          {selectedMemory && (
            <div className="mt-6 space-y-4">
              <div className="space-y-1">
                <Label className="text-xs text-onSurface-default-tertiary">
                  内容
                </Label>
                <p className="text-sm">{selectedMemory.memory}</p>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-1">
                  <Label className="text-xs text-onSurface-default-tertiary">
                    ID
                  </Label>
                  <p className="text-xs font-mono break-all">
                    {selectedMemory.id}
                  </p>
                </div>
                {selectedMemory.user_id && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      用户
                    </Label>
                    <p className="text-sm">{selectedMemory.user_id}</p>
                  </div>
                )}
                {selectedMemory.agent_id && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      智能体
                    </Label>
                    <p className="text-sm">{selectedMemory.agent_id}</p>
                  </div>
                )}
                {selectedMemory.tenant_id && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      租户
                    </Label>
                    <p className="text-sm">{selectedMemory.tenant_id}</p>
                  </div>
                )}
                {selectedMemory.session_id && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      会话
                    </Label>
                    <p className="text-sm">{selectedMemory.session_id}</p>
                  </div>
                )}
                {selectedMemory.created_at && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      创建时间
                    </Label>
                    <p className="text-sm">
                      {new Date(selectedMemory.created_at).toLocaleString()}
                    </p>
                  </div>
                )}
                {(selectedMemory.metadata?.categories?.length ?? 0) > 0 && (
                  <div className="col-span-2 space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      分类
                    </Label>
                    <div className="flex flex-wrap gap-1">
                      {selectedMemory.metadata?.categories?.map((category) => (
                        <Badge
                          key={category}
                          variant="secondary"
                          className="px-1.5 py-0 text-xs font-normal"
                        >
                          {category}
                        </Badge>
                      ))}
                    </div>
                  </div>
                )}
              </div>
              <Button
                variant="outline"
                size="sm"
                className="text-onSurface-danger-primary"
                onClick={() => setMemoryToDelete(selectedMemory)}
              >
                <Trash2 className="size-3.5 mr-1" />
                删除记忆
              </Button>
            </div>
          )}
        </SheetContent>
      </Sheet>

      <DeleteConfirmationModal
        isOpen={!!memoryToDelete}
        onClose={() => setMemoryToDelete(null)}
        onConfirm={handleDelete}
        title="删除记忆"
        description="该记忆将被永久删除，此操作无法撤销。"
        itemName={memoryToDelete?.id ?? ""}
        confirmButtonText="删除"
      />
    </div>
  );
}
