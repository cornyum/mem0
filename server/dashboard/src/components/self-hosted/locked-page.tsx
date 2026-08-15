"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Lock } from "lucide-react";

interface LockedPageProps {
  title: string;
  description: string;
  previewContent: React.ReactNode;
  utmMedium: string;
}

export function LockedPage({
  title,
  description,
  previewContent,
}: LockedPageProps) {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold font-fustat flex items-center gap-2">
          {title}
          <Lock className="size-4 text-onSurface-default-tertiary" />
        </h1>
        <p className="text-sm text-onSurface-default-secondary mt-1">
          {description}
        </p>
      </div>

      <div className="opacity-60 pointer-events-none select-none">
        {previewContent}
      </div>

      <Card className="border-memBorder-primary">
        <CardContent className="flex flex-col sm:flex-row items-center gap-4 py-6">
          <div className="flex-1">
            <p className="text-sm font-medium">该能力当前版本暂未开放。</p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
