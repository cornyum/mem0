"use client";

import { LockedPage } from "@/components/self-hosted/locked-page";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";

function WebhooksMockup() {
  return (
    <Card className="border-memBorder-primary">
      <CardContent className="p-6 space-y-4">
        <div className="space-y-2">
          <Label>端点 URL</Label>
          <Input placeholder="https://your-app.com/webhooks" disabled />
        </div>
        <div className="space-y-2">
          <Label>事件</Label>
          <div className="flex flex-col gap-2">
            {[
              "memory.created",
              "memory.updated",
              "memory.deleted",
              "search.performed",
            ].map((event) => (
              <div key={event} className="flex items-center gap-2">
                <Checkbox disabled checked={event === "memory.created"} />
                <Label className="text-sm font-normal">{event}</Label>
              </div>
            ))}
          </div>
        </div>
        <div className="space-y-2">
          <Label>签名密钥</Label>
          <Input placeholder="whsec_..." disabled type="password" />
        </div>
        <Button disabled>创建 Webhook</Button>
      </CardContent>
    </Card>
  );
}

export default function WebhooksPage() {
  return (
    <LockedPage
      title="Webhook"
      description="在记忆被创建、更新或删除时收到通知。"
      previewContent={<WebhooksMockup />}
      utmMedium="dashboard-locked-webhooks"
    />
  );
}
