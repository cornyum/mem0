"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Check, Copy } from "lucide-react";
import { CopyToClipboard } from "react-copy-to-clipboard";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/hooks/use-auth";
import { getErrorMessage } from "@/lib/error-message";
import { isValidEmail } from "@/lib/validators";

const RESET_COMMAND =
  "make reset-admin-password EMAIL=<your-email> PASSWORD=<new-password>";

export default function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user, isLoading, login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!isLoading && user) {
      router.push(searchParams.get("next") || "/dashboard/requests");
    }
  }, [user, isLoading, router, searchParams]);

  const emailValid = isValidEmail(email);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (!emailValid) {
      setError("请输入有效的邮箱地址。");
      return;
    }
    setSubmitting(true);
    try {
      await login(email, password);
      router.push(searchParams.get("next") || "/dashboard/requests");
    } catch (err) {
      setError(getErrorMessage(err, "登录失败"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen">
      <div className="flex-1 bg-surface-default-primary flex items-center justify-center p-8">
        <div className="w-full max-w-md">
          <div className="flex justify-center mb-2">
            <span className="text-xl font-bold font-fustat text-onSurface-default-primary">
              Agentar 记忆平台
            </span>
          </div>
          <h1 className="text-2xl font-semibold text-onSurface-default-primary text-center mb-6 font-fustat">
            登录 Agentar 记忆平台
          </h1>
          <div className="flex flex-col gap-4 border p-8 border-memBorder-primary rounded-xl">
            {error && (
              <p className="text-sm text-onSurface-danger-primary bg-surface-danger-primary px-3 py-2 rounded">
                {error}
              </p>
            )}
            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              <div className="space-y-1.5">
                <Label htmlFor="login-email">邮箱</Label>
                <Input
                  id="login-email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="admin@company.com"
                  required
                  autoFocus
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="login-password">密码</Label>
                <Input
                  id="login-password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
              </div>
              <Button
                type="submit"
                disabled={submitting || !emailValid || !password}
                variant="default"
                size="lg"
                className="w-full"
              >
                {submitting ? "登录中..." : "登录"}
              </Button>
            </form>
            <Dialog>
              <DialogTrigger asChild>
                <button
                  type="button"
                  className="text-xs text-onSurface-default-tertiary hover:text-onSurface-default-primary underline underline-offset-4 self-center"
                >
                  忘记密码？
                </button>
              </DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>重置管理员密码</DialogTitle>
                  <DialogDescription>
                    请在服务器主机上执行以下命令。该命令将覆盖现有密码；已登录用户在其会话过期前仍保持登录状态。
                  </DialogDescription>
                </DialogHeader>
                <div className="flex gap-2">
                  <Input
                    readOnly
                    value={RESET_COMMAND}
                    className="font-mono text-xs"
                  />
                  <CopyToClipboard
                    text={RESET_COMMAND}
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
              </DialogContent>
            </Dialog>
          </div>
        </div>
      </div>

      <div className="relative hidden h-screen flex-1 items-center justify-center overflow-hidden bg-gradient-to-b from-[#31275A] to-[#5C49A3] px-10 lg:flex">
        <div className="pointer-events-none absolute inset-0 bg-[url('/images/dither.svg')] bg-bottom bg-no-repeat bg-contain" />
        <div className="relative z-10 flex w-full max-w-[564px] flex-col items-center gap-8 text-center text-white">
          <p className="typo-h3 text-white">Agentar 记忆平台</p>
          <div className="space-y-2">
            <p className="typo-body text-white">
              为 AI 智能体提供持久化、个性化的记忆层，
            </p>
            <p className="typo-body text-white">
              支持多租户隔离与全私有化部署。
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
