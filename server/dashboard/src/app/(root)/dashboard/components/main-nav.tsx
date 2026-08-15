"use client";

import * as React from "react";
import Link from "next/link";
import {
  Activity,
  ChartLine,
  ChevronDown,
  FolderInput,
  GalleryVerticalEnd,
  KeyRound,
  Settings,
  Tags,
  Users,
  WebhookIcon,
  Wrench,
} from "lucide-react";
import { useSelector } from "react-redux";
import { RootState } from "@/store/store";
import { Badge } from "@/components/ui/badge";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  SidebarGroupLabel,
} from "@/components/ui/sidebar";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

export function MainNav({
  className,
  ...props
}: React.HTMLAttributes<HTMLElement>) {
  const pathname = usePathname();
  const isSidebarCollapsed = useSelector(
    (state: RootState) => state.layout.isSidebarCollapsed,
  );
  const [isCloudOpen, setIsCloudOpen] = React.useState(true);

  return (
    <Sidebar
      collapsible={isSidebarCollapsed ? "icon" : undefined}
      className={cn(className, "border-r-0 w-full mb-0 bg-transparent")}
      {...props}
    >
      <SidebarContent>
        <SidebarGroup>
          <SidebarMenu className="gap-0">
            <div className="flex flex-col gap-3">
              <div className="flex flex-col gap-0">
                {!isSidebarCollapsed && (
                  <SidebarGroupLabel className="mb-0">动态</SidebarGroupLabel>
                )}
                {[
                  {
                    title: "请求",
                    url: "/dashboard/requests",
                    icon: Activity,
                    active: pathname === "/dashboard/requests",
                  },
                  {
                    title: "记忆",
                    url: "/dashboard/memories",
                    icon: GalleryVerticalEnd,
                    active: pathname === "/dashboard/memories",
                  },
                  {
                    title: "实体",
                    url: "/dashboard/entities",
                    icon: Users,
                    active: pathname === "/dashboard/entities",
                  },
                ].map((item) => (
                  <SidebarMenuItem key={item.title}>
                    <SidebarMenuButton
                      asChild
                      collapsed={isSidebarCollapsed}
                      active={item.active}
                      tooltip={isSidebarCollapsed ? item.title : undefined}
                    >
                      <Link
                        href={item.url}
                        className={cn(
                          "flex items-center w-full",
                          isSidebarCollapsed
                            ? "justify-center mx-auto"
                            : "gap-1.5",
                        )}
                      >
                        <item.icon className="size-4 shrink-0" />
                        {!isSidebarCollapsed && <span>{item.title}</span>}
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </div>

              {isSidebarCollapsed && (
                <div className="h-[1px] w-full bg-memBorder-primary my-2" />
              )}

              <Collapsible
                open={isCloudOpen}
                onOpenChange={setIsCloudOpen}
                className="flex flex-col gap-0"
              >
                {!isSidebarCollapsed && (
                  <CollapsibleTrigger asChild>
                    <SidebarGroupLabel className="cursor-pointer mb-0">
                      进阶能力
                      <ChevronDown
                        className={cn(
                          "size-3 transition-transform duration-200",
                          isCloudOpen ? "" : "-rotate-90",
                        )}
                      />
                    </SidebarGroupLabel>
                  </CollapsibleTrigger>
                )}
                <CollapsibleContent className="flex flex-col gap-0">
                  {[
                    {
                      title: "分类",
                      url: "/dashboard/categories",
                      icon: Tags,
                    },
                    {
                      title: "Webhook",
                      url: "/dashboard/webhooks",
                      icon: WebhookIcon,
                      showPro: true,
                    },
                    {
                      title: "分析",
                      url: "/dashboard/analytics",
                      icon: ChartLine,
                      showPro: true,
                    },
                    {
                      title: "导出",
                      url: "/dashboard/export",
                      icon: FolderInput,
                    },
                  ].map((item) => (
                    <SidebarMenuItem key={item.title}>
                      <SidebarMenuButton
                        asChild
                        collapsed={isSidebarCollapsed}
                        active={pathname === item.url}
                        tooltip={isSidebarCollapsed ? item.title : undefined}
                      >
                        <Link
                          href={item.url}
                          className={cn(
                            "flex items-center w-full",
                            isSidebarCollapsed
                              ? "justify-center mx-auto"
                              : "gap-1.5",
                          )}
                        >
                          <item.icon className="size-4 shrink-0" />
                          {!isSidebarCollapsed && (
                            <>
                              <span>{item.title}</span>
                              {item.showPro && (
                                <Badge
                                  variant="outline"
                                  className="ml-auto text-memGold-600 border-memGold-300 typo-caption-sm px-1.5 py-0"
                                >
                                  PRO
                                </Badge>
                              )}
                            </>
                          )}
                        </Link>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  ))}
                </CollapsibleContent>
              </Collapsible>

              {isSidebarCollapsed && (
                <div className="h-[1px] w-full bg-memBorder-primary my-2" />
              )}

              <div className="flex flex-col gap-0">
                {!isSidebarCollapsed && (
                  <SidebarGroupLabel className="mb-0">账户</SidebarGroupLabel>
                )}
                {[
                  {
                    title: "API 密钥",
                    url: "/dashboard/api-keys",
                    icon: KeyRound,
                    active: pathname === "/dashboard/api-keys",
                  },
                  {
                    title: "配置",
                    url: "/dashboard/configuration",
                    icon: Wrench,
                    active: pathname === "/dashboard/configuration",
                  },
                  {
                    title: "设置",
                    url: "/dashboard/settings",
                    icon: Settings,
                    active: pathname === "/dashboard/settings",
                  },
                ].map((item) => (
                  <SidebarMenuItem key={item.title}>
                    <SidebarMenuButton
                      asChild
                      collapsed={isSidebarCollapsed}
                      active={item.active}
                      tooltip={isSidebarCollapsed ? item.title : undefined}
                    >
                      <Link
                        href={item.url}
                        className={cn(
                          "flex items-center w-full",
                          isSidebarCollapsed
                            ? "justify-center mx-auto"
                            : "gap-1.5",
                        )}
                      >
                        <item.icon className="size-4 shrink-0" />
                        {!isSidebarCollapsed && <span>{item.title}</span>}
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </div>
            </div>
          </SidebarMenu>
        </SidebarGroup>
      </SidebarContent>
      <SidebarRail />
    </Sidebar>
  );
}
