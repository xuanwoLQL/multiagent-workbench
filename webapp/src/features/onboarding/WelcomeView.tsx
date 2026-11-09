import { useState } from "react";
import { FolderGit2, ListChecks, ShieldCheck } from "lucide-react";
import { useProjects } from "../../app/queries";
import { Badge } from "../../ui/primitives";
import { ProjectPicker } from "./ProjectPicker";
import { StartWorkPanel } from "./StartWorkPanel";
import { desktopBridge, type ProjectRecord } from "./project-api";
import "./onboarding.css";

export function WelcomeView({ onImported }: { onImported: (id: string) => void }) {
  const projectsQuery = useProjects();
  const [opened, setOpened] = useState<ProjectRecord | null>(null);
  const [localProjects, setLocalProjects] = useState<ProjectRecord[]>([]);

  const projects = mergeProjects(projectsQuery.data ?? [], localProjects);
  const desktop = desktopBridge();

  const handleOpened = (project: ProjectRecord) => {
    setLocalProjects((previous) => mergeProjects(previous, [project]));
    setOpened(project);
  };

  return (
    <div className="ob-page">
      <header className="ob-top">
        <div className="ob-brand">
          <span className="brand-mark brand-mark-lg">M</span>
          <div>
            <div className="ob-brand-name">MultiAgent</div>
            <div className="ob-brand-sub">多 Agent 协作工作台</div>
          </div>
        </div>
        <Badge tone="outline" dot>
          {desktop?.isDesktop ? "桌面端 · 本地服务" : "浏览器 · 本地服务"}
        </Badge>
      </header>

      <main className="ob-main">
        {opened ? (
          <StartWorkPanel
            project={opened}
            onEnter={onImported}
            onBack={() => setOpened(null)}
          />
        ) : (
          <div className="ob-grid">
            <section className="ob-hero" aria-labelledby="ob-hero-title">
              <h1 id="ob-hero-title">把一个真实项目交给 Agent 团队</h1>
              <p className="ob-hero-lede">
                打开本地目录，描述你想完成的事。工作台会把目标拆成可执行的任务，
                分配 Agent，在隔离副本里执行，并把每一次改动留给你审阅。
              </p>

              <div className="ob-points">
                <div className="ob-point">
                  <span className="ob-point-icon">
                    <FolderGit2 size={15} />
                  </span>
                  <div>
                    <strong>真实目录，不是演示沙盒</strong>
                    <p>
                      直接使用你已有的代码库；Git 仓库会为任务创建独立 worktree，
                      验收通过后再同步回工作区。
                    </p>
                  </div>
                </div>
                <div className="ob-point">
                  <span className="ob-point-icon">
                    <ListChecks size={15} />
                  </span>
                  <div>
                    <strong>任务有依赖、验收和重试预算</strong>
                    <p>
                      一个目标拆成多步计划，每步都有产物与验收条件；失败会按预算自动再试，
                      不会静默停在半路。
                    </p>
                  </div>
                </div>
                <div className="ob-point">
                  <span className="ob-point-icon">
                    <ShieldCheck size={15} />
                  </span>
                  <div>
                    <strong>危险操作先停下来</strong>
                    <p>
                      写范围、人工确认、版本快照与回滚都在同一处；
                      所有协作数据只写入这台机器。
                    </p>
                  </div>
                </div>
              </div>

              <div className="ob-hero-foot">
                <span>本地优先</span>
                <span>不使用云端索引</span>
                <span>支持多项目并存</span>
              </div>
            </section>

            <ProjectPicker
              projects={projects}
              loading={projectsQuery.isLoading}
              error={projectsQuery.isError ? projectsQuery.error : null}
              onRetry={() => void projectsQuery.refetch()}
              onOpen={handleOpened}
              onOpened={handleOpened}
            />
          </div>
        )}
      </main>

      <footer className="ob-foot">
        <span>数据只写入本机</span>
        <span className="ob-foot-dot" />
        <span>项目目录中的文件不会被移动</span>
        <span className="ob-foot-spacer" />
      </footer>
    </div>
  );
}

function mergeProjects(base: ProjectRecord[], extra: ProjectRecord[]): ProjectRecord[] {
  const seen = new Set(base.map((project) => project.id));
  return [...base, ...extra.filter((project) => !seen.has(project.id))];
}

