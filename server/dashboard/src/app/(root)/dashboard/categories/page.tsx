"use client";

import { useState } from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DataTable } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import { toast } from "@/components/ui/use-toast";
import { getErrorMessage } from "@/lib/error-message";
import { api } from "@/utils/api";
import { CATEGORY_ENDPOINTS } from "@/utils/api-endpoints";
import { useApiQuery } from "@/hooks/use-api-query";
import { Category, CategoryListResponse } from "@/types/api";

// Keep in sync with the server-side validation in PUT /categories.
const MAX_CATEGORIES = 50;
const NAME_MAX_LENGTH = 64;
const DESCRIPTION_MAX_LENGTH = 200;

export default function CategoriesPage() {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingCategory, setEditingCategory] = useState<Category | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);
  const [categoryToDelete, setCategoryToDelete] = useState<Category | null>(
    null,
  );

  const {
    data: categories = [],
    isLoading,
    refetch,
  } = useApiQuery<Category[]>(
    async () => {
      const res = await api.get<CategoryListResponse>(CATEGORY_ENDPOINTS.BASE);
      return Array.isArray(res.data?.categories) ? res.data.categories : [];
    },
    { errorToast: "加载分类失败", initialData: [] },
  );

  const resetForm = () => {
    setEditingCategory(null);
    setName("");
    setDescription("");
    setFormError("");
  };

  const openCreate = () => {
    if (categories.length >= MAX_CATEGORIES) {
      toast({
        title: "无法新增分类",
        description: `最多支持 ${MAX_CATEGORIES} 个分类。`,
        variant: "destructive",
      });
      return;
    }
    resetForm();
    setDialogOpen(true);
  };

  const openEdit = (category: Category) => {
    setEditingCategory(category);
    setName(category.name);
    setDescription(category.description ?? "");
    setFormError("");
    setDialogOpen(true);
  };

  const handleDialogChange = (open: boolean) => {
    if (!open) resetForm();
    setDialogOpen(open);
  };

  const validate = (): string => {
    const trimmedName = name.trim();
    if (!trimmedName) return "名称不能为空";
    if (trimmedName.length > NAME_MAX_LENGTH)
      return `名称不能超过 ${NAME_MAX_LENGTH} 个字符`;
    const duplicated = categories.some(
      (c) => c.name !== editingCategory?.name && c.name.trim() === trimmedName,
    );
    if (duplicated) return "该名称已存在";
    if (description.trim().length > DESCRIPTION_MAX_LENGTH)
      return `描述不能超过 ${DESCRIPTION_MAX_LENGTH} 个字符`;
    return "";
  };

  const handleSave = async () => {
    const error = validate();
    if (error) {
      setFormError(error);
      return;
    }
    const trimmedName = name.trim();
    const trimmedDescription = description.trim();
    // PUT /categories 是全量提交：基于最新列表组装请求体。
    const nextCategories = editingCategory
      ? categories.map((c) =>
          c.name === editingCategory.name
            ? { name: trimmedName, description: trimmedDescription }
            : c,
        )
      : [...categories, { name: trimmedName, description: trimmedDescription }];

    setSaving(true);
    try {
      await api.put(CATEGORY_ENDPOINTS.BASE, {
        categories: nextCategories,
      });
      toast({
        title: editingCategory ? "分类已更新" : "分类已创建",
        variant: "success",
      });
      handleDialogChange(false);
      void refetch();
    } catch (err) {
      toast({
        title: "保存分类失败",
        description: getErrorMessage(err),
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!categoryToDelete) return;
    const nextCategories = categories.filter(
      (c) => c.name !== categoryToDelete.name,
    );
    try {
      await api.put(CATEGORY_ENDPOINTS.BASE, {
        categories: nextCategories,
      });
      toast({ title: "分类已删除", variant: "success" });
      setCategoryToDelete(null);
      void refetch();
    } catch (err) {
      toast({
        title: "删除分类失败",
        description: getErrorMessage(err),
        variant: "destructive",
      });
    }
  };

  const columns = [
    {
      key: "name" as keyof Category,
      label: "名称",
      width: 120,
      render: (value: string) => (
        <span className="text-sm font-medium">{value}</span>
      ),
    },
    {
      key: "description" as keyof Category,
      label: "描述",
      width: 320,
      render: (value: string) =>
        value ? (
          <span className="line-clamp-2 text-sm text-onSurface-default-secondary">
            {value}
          </span>
        ) : (
          "--"
        ),
    },
    {
      key: "name" as keyof Category,
      label: "操作",
      width: 90,
      render: (_value: Category[keyof Category], row: Category) => (
        <div className="flex justify-end gap-1">
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            onClick={() => openEdit(row)}
          >
            <Pencil className="size-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            onClick={() => setCategoryToDelete(row)}
          >
            <Trash2 className="size-3.5 text-onSurface-danger-primary" />
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold font-fustat">自定义分类</h1>
        <Button size="sm" onClick={openCreate}>
          <Plus className="size-4 mr-1" /> 新增分类
        </Button>
      </div>

      <Card className="border-memBorder-primary">
        <CardContent className="p-4 text-xs text-onSurface-default-secondary">
          分类由 LLM
          在记忆写入时自动打标，仅对新写入的记忆生效，存量记忆不会回填。最多定义
          {MAX_CATEGORIES} 个分类。
        </CardContent>
      </Card>

      {isLoading ? (
        <TableSkeleton rows={4} columns={3} />
      ) : categories.length === 0 ? (
        <EmptyState
          title="尚未定义分类"
          description="定义一套分类体系后，LLM 会在记忆写入时自动为每条记忆打上分类标签。"
        >
          <Button size="sm" className="mt-4" onClick={openCreate}>
            <Plus className="size-4 mr-1" /> 新增分类
          </Button>
        </EmptyState>
      ) : (
        <Card className="border-memBorder-primary overflow-hidden">
          <DataTable
            data={categories}
            columns={columns}
            getRowKey={(row) => row.name}
          />
        </Card>
      )}

      <Dialog open={dialogOpen} onOpenChange={handleDialogChange}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {editingCategory ? "编辑分类" : "新增分类"}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-4 mt-2">
            <div className="space-y-2">
              <Label htmlFor="category-name">
                名称<span className="text-onSurface-danger-primary">*</span>
              </Label>
              <Input
                id="category-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="例如：健康"
                maxLength={NAME_MAX_LENGTH}
              />
              <p className="text-xs text-onSurface-default-tertiary">
                {name.length}/{NAME_MAX_LENGTH}
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="category-description">描述（选填）</Label>
              <Textarea
                id="category-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="帮助 LLM 判断记忆是否属于该分类的依据"
                maxLength={DESCRIPTION_MAX_LENGTH}
                rows={3}
              />
              <p className="text-xs text-onSurface-default-tertiary">
                {description.length}/{DESCRIPTION_MAX_LENGTH}
              </p>
            </div>
            {formError && (
              <p className="text-xs text-onSurface-danger-primary">
                {formError}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button
                variant="outline"
                onClick={() => handleDialogChange(false)}
              >
                取消
              </Button>
              <Button onClick={handleSave} disabled={saving}>
                {saving ? "保存中..." : "保存"}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <DeleteConfirmationModal
        isOpen={!!categoryToDelete}
        onClose={() => setCategoryToDelete(null)}
        onConfirm={handleDelete}
        title="删除分类"
        description="删除后，新写入的记忆将不再被打上该分类标签，此操作无法撤销。"
        itemName={categoryToDelete?.name ?? ""}
        confirmButtonText="删除"
      />
    </div>
  );
}
