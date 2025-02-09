import type { TemplateResult } from "lit";
import { render } from "lit";
import { property, state, query, customElement } from "lit/decorators.js";
import { when } from "lit/directives/when.js";
import { msg, localized } from "@lit/localize";
import { ifDefined } from "lit/directives/if-defined.js";
import type { SlDialog } from "@shoelace-style/shoelace";
import "broadcastchannel-polyfill";

import "./utils/polyfills";
import appState, { use, AppStateService } from "./utils/state";
import type { OrgTab } from "./pages/org";
import type { NavigateEventDetail } from "@/controllers/navigate";
import type { NotifyEventDetail } from "@/controllers/notify";
import LiteElement, { html } from "./utils/LiteElement";
import APIRouter from "./utils/APIRouter";
import AuthService from "./utils/AuthService";
import type {
  LoggedInEventDetail,
  NeedLoginEventDetail,
  AuthState,
  Auth,
} from "./utils/AuthService";
import type { ViewState } from "./utils/APIRouter";
import type { CurrentUser, UserOrg } from "./types/user";
import type { AuthStorageEventDetail } from "./utils/AuthService";
import { ROUTES } from "./routes";
import "./shoelace";
import "./components";
import "./features";
import "./pages";
import "./assets/fonts/Inter/inter.css";
import "./assets/fonts/Recursive/recursive.css";
import "./styles.css";
import { theme } from "@/theme";

// Make theme CSS available in document
document.adoptedStyleSheets = [theme];

type DialogContent = {
  label?: TemplateResult | string;
  body?: TemplateResult | string;
  noHeader?: boolean;
};

export type APIUser = {
  id: string;
  email: string;
  name: string;
  is_verified: boolean;
  is_superuser: boolean;
  orgs: UserOrg[];
};

@localized()
@customElement("browsertrix-app")
export class App extends LiteElement {
  @property({ type: String })
  version?: string;

  private readonly router = new APIRouter(ROUTES);
  authService = new AuthService();

  @use()
  appState = appState;

  @state()
  private viewState!: ViewState;

  @state()
  private globalDialogContent: DialogContent = {};

  @query("#globalDialog")
  private readonly globalDialog!: SlDialog;

  @state()
  private isAppSettingsLoaded = false;

  @state()
  private isRegistrationEnabled?: boolean;

  async connectedCallback() {
    let authState: AuthState = null;
    try {
      authState = await AuthService.initSessionStorage();
    } catch (e) {
      console.debug(e);
    }
    this.syncViewState();
    if (this.viewState.route === "org") {
      AppStateService.updateOrgSlug(this.viewState.params.slug || null);
    }
    if (authState) {
      this.authService.saveLogin(authState);
      void this.updateUserInfo();
    }
    super.connectedCallback();

    this.addEventListener("btrix-navigate", this.onNavigateTo);
    this.addEventListener("btrix-notify", this.onNotify);
    this.addEventListener("btrix-need-login", this.onNeedLogin);
    this.addEventListener("btrix-logged-in", this.onLoggedIn);
    this.addEventListener("btrix-log-out", this.onLogOut);
    window.addEventListener("popstate", () => {
      this.syncViewState();
    });

    this.startSyncBrowserTabs();
    void this.fetchAppSettings();
  }

  willUpdate(changedProperties: Map<string, unknown>) {
    if (changedProperties.get("viewState") && this.viewState.route === "org") {
      AppStateService.updateOrgSlug(this.viewState.params.slug || null);
    }
  }

  private syncViewState() {
    if (
      this.authService.authState &&
      (window.location.pathname === "/log-in" ||
        window.location.pathname === "/reset-password")
    ) {
      // Redirect to logged in home page
      this.viewState = this.router.match(ROUTES.home);
      window.history.replaceState(this.viewState, "", this.viewState.pathname);
    } else {
      this.viewState = this.router.match(
        `${window.location.pathname}${window.location.search}`,
      );
    }
  }

  private async fetchAppSettings() {
    const settings = await this.getAppSettings();

    if (settings) {
      this.isRegistrationEnabled = settings.registrationEnabled;
    }

    this.isAppSettingsLoaded = true;
  }

  private async updateUserInfo() {
    try {
      const userInfo = await this.getUserInfo();
      AppStateService.updateUserInfo({
        id: userInfo.id,
        email: userInfo.email,
        name: userInfo.name,
        isVerified: userInfo.is_verified,
        isAdmin: userInfo.is_superuser,
        orgs: userInfo.orgs,
      });
      const orgs = userInfo.orgs;
      if (
        orgs.length &&
        !this.appState.userInfo!.isAdmin &&
        !this.appState.orgSlug
      ) {
        const firstOrg = orgs[0].slug;
        AppStateService.updateOrgSlug(firstOrg);
      }
    } catch (err) {
      if ((err as Error)?.message === "Unauthorized") {
        console.debug(
          "Unauthorized with authState:",
          this.authService.authState,
        );
        this.clearUser();
        this.navigate(ROUTES.login);
      }
    }
  }

  async getAppSettings(): Promise<{ registrationEnabled: boolean } | void> {
    const resp = await fetch("/api/settings", {
      headers: { "Content-Type": "application/json" },
    });

    if (resp.status === 200) {
      const body = (await resp.json()) as { registrationEnabled: boolean };

      return body;
    } else {
      console.debug(resp);
    }
  }

  navigate(newViewPath: string, state?: { [key: string]: unknown }) {
    let url;

    if (newViewPath.startsWith("http")) {
      url = new URL(newViewPath);
    } else {
      url = new URL(
        `${window.location.origin}/${newViewPath.replace(/^\//, "")}`,
      );
    }

    // Remove hash from path for matching
    newViewPath = `${url.pathname}${url.search}`;

    if (newViewPath === "/log-in" && this.authService.authState) {
      // Redirect to logged in home page
      this.viewState = this.router.match(ROUTES.home);
    } else {
      this.viewState = this.router.match(newViewPath);
    }

    this.viewState.data = state;

    window.history.pushState(
      this.viewState,
      "",
      `${this.viewState.pathname.replace(url.search, "")}${url.hash}${
        url.search
      }`,
    );
  }

  render() {
    return html`
      <div class="min-w-screen flex min-h-screen flex-col">
        ${this.renderNavBar()}
        <main class="relative flex flex-auto">${this.renderPage()}</main>
        <div class="border-t border-neutral-100">${this.renderFooter()}</div>
      </div>

      <sl-dialog
        id="globalDialog"
        ?noHeader=${this.globalDialogContent?.noHeader === true}
        label=${this.globalDialogContent?.label || msg("Message")}
        @sl-after-hide=${() => (this.globalDialogContent = {})}
        >${this.globalDialogContent?.body}</sl-dialog
      >
    `;
  }

  private renderNavBar() {
    const isAdmin = this.appState.userInfo?.isAdmin;
    let homeHref = "/";
    if (!isAdmin && this.appState.orgSlug) {
      homeHref = `/orgs/${this.appState.orgSlug}`;
    }

    return html`
      <div class="border-b">
        <nav
          class="mx-auto box-border flex h-12 max-w-screen-desktop items-center justify-between pl-3"
        >
          <div>
            <a
              class="text-sm font-medium hover:text-neutral-400"
              href=${homeHref}
              @click=${(e: MouseEvent) => {
                if (isAdmin) {
                  this.clearSelectedOrg();
                }
                this.navLink(e);
              }}
            >
              ${msg("Browsertrix Cloud")}
            </a>
          </div>

          ${isAdmin
            ? html`
                <div
                  class="grid grid-flow-col items-center gap-3 text-xs md:gap-5 md:text-sm"
                >
                  <a
                    class="font-medium text-neutral-500 hover:text-neutral-400"
                    href="/"
                    @click=${(e: MouseEvent) => {
                      this.clearSelectedOrg();
                      this.navLink(e);
                    }}
                    >${msg("Dashboard")}</a
                  >
                  <a
                    class="font-medium text-neutral-500 hover:text-neutral-400"
                    href="/crawls"
                    @click=${this.navLink}
                    >${msg("Running Crawls")}</a
                  >
                  <div class="hidden md:block">${this.renderFindCrawl()}</div>
                </div>
              `
            : ""}

          <div class="grid auto-cols-max grid-flow-col items-center gap-3">
            ${this.authService.authState
              ? html` ${this.renderOrgs()}
                  <sl-dropdown placement="bottom-end">
                    <sl-icon-button
                      slot="trigger"
                      name="person-circle"
                      label=${msg("Open user menu")}
                      style="font-size: 1.5rem;"
                    ></sl-icon-button>

                    <sl-menu class="w-60 min-w-min max-w-full">
                      <div class="px-7 py-2">${this.renderMenuUserInfo()}</div>
                      <sl-divider></sl-divider>
                      <sl-menu-item
                        @click=${() => this.navigate(ROUTES.accountSettings)}
                      >
                        <sl-icon slot="prefix" name="gear"></sl-icon>
                        ${msg("Account Settings")}
                      </sl-menu-item>
                      ${this.appState.userInfo?.isAdmin
                        ? html` <sl-menu-item
                            @click=${() => this.navigate(ROUTES.usersInvite)}
                          >
                            <sl-icon slot="prefix" name="person-plus"></sl-icon>
                            ${msg("Invite Users")}
                          </sl-menu-item>`
                        : ""}
                      <sl-divider></sl-divider>
                      <sl-menu-item @click="${this.onLogOut}">
                        <sl-icon slot="prefix" name="door-open"></sl-icon>
                        ${msg("Log Out")}
                      </sl-menu-item>
                    </sl-menu>
                  </sl-dropdown>`
              : html`
                  <a href="/log-in"> ${msg("Log In")} </a>
                  ${this.isRegistrationEnabled
                    ? html`
                        <sl-button
                          variant="text"
                          @click="${() => this.navigate("/sign-up")}"
                        >
                          ${msg("Sign up")}
                        </sl-button>
                      `
                    : html``}
                `}
          </div>
        </nav>
      </div>
    `;
  }

  private renderOrgs() {
    const orgs = this.appState.userInfo?.orgs;
    if (!orgs || orgs.length < 2 || !this.appState.userInfo) return;

    const selectedOption = this.appState.orgSlug
      ? orgs.find(({ slug }) => slug === this.appState.orgSlug)
      : { slug: "", name: msg("All Organizations") };
    if (!selectedOption) {
      console.debug(
        `Could't find organization with slug ${this.appState.orgSlug}`,
        orgs,
      );
      return;
    }

    // Limit org name display for orgs created before org name max length restriction
    const orgNameLength = 50;

    return html`
      <sl-dropdown placement="bottom-end">
        <sl-button slot="trigger" variant="text" size="small" caret
          >${selectedOption.name.slice(0, orgNameLength)}</sl-button
        >
        <sl-menu
          @sl-select=${(e: CustomEvent<{ item: { value: string } }>) => {
            const { value } = e.detail.item;
            if (value) {
              this.navigate(`/orgs/${value}`);
            } else {
              if (this.appState.userInfo) {
                this.clearSelectedOrg();
              }
              this.navigate(`/`);
            }
          }}
        >
          ${when(
            this.appState.userInfo.isAdmin,
            () => html`
              <sl-menu-item
                type="checkbox"
                value=""
                ?checked=${!selectedOption.slug}
                >${msg("All Organizations")}</sl-menu-item
              >
              <sl-divider></sl-divider>
            `,
          )}
          ${this.appState.userInfo?.orgs.map(
            (org) => html`
              <sl-menu-item
                type="checkbox"
                value=${org.slug}
                ?checked=${org.slug === selectedOption.slug}
                >${org.name.slice(0, orgNameLength)}</sl-menu-item
              >
            `,
          )}
        </sl-menu>
      </sl-dropdown>
    `;
  }

  private renderMenuUserInfo() {
    if (!this.appState.userInfo) return;
    if (this.appState.userInfo.isAdmin) {
      return html`
        <div class="mb-2">
          <sl-tag class="uppercase" variant="primary" size="small"
            >${msg("admin")}</sl-tag
          >
        </div>
        <div class="font-medium text-neutral-700">
          ${this.appState.userInfo?.name}
        </div>
        <div class="whitespace-nowrap text-xs text-neutral-500">
          ${this.appState.userInfo?.email}
        </div>
      `;
    }

    const orgs = this.appState.userInfo?.orgs;
    if (orgs?.length === 1) {
      return html`
        <div class="my-1 font-medium text-neutral-700">${orgs[0].name}</div>
        <div class="text-neutral-500">${this.appState.userInfo?.name}</div>
        <div class="whitespace-nowrap text-xs text-neutral-500">
          ${this.appState.userInfo?.email}
        </div>
      `;
    }

    return html`
      <div class="font-medium text-neutral-700">
        ${this.appState.userInfo?.name}
      </div>
      <div class="whitespace-nowrap text-xs text-neutral-500">
        ${this.appState.userInfo?.email}
      </div>
    `;
  }

  private renderFooter() {
    return html`
      <footer
        class="mx-auto box-border flex w-full max-w-screen-desktop flex-col justify-between gap-4 p-3 md:flex-row"
      >
        <!-- <div> -->
        <!-- TODO re-enable when translations are added -->
        <!-- <btrix-locale-picker></btrix-locale-picker> -->
        <!-- </div> -->
        <div class="flex items-center justify-center">
          <a
            class="flex items-center gap-2 text-neutral-400 hover:text-primary"
            href="https://github.com/webrecorder/browsertrix-cloud"
            target="_blank"
            rel="noopener"
          >
            <sl-icon
              name="github"
              class="inline-block align-middle text-base"
            ></sl-icon>
            Source Code
          </a>
        </div>
        <div class="flex items-center justify-center">
          <a
            class="flex items-center gap-2 text-neutral-400 hover:text-primary"
            href="https://docs.browsertrix.cloud"
            target="_blank"
            rel="noopener"
          >
            <sl-icon
              name="book-half"
              class="inline-block align-middle text-base"
            ></sl-icon>
            Documentation
          </a>
        </div>
        <div class="flex items-center justify-center">
          ${this.version
            ? html`
                <btrix-copy-button
                  class="text-neutral-400"
                  .getValue=${() => this.version}
                  content=${msg("Copy Version Code")}
                ></btrix-copy-button>
                <span
                  class="font-monostyle inline-block align-middle text-xs text-neutral-400"
                >
                  ${this.version}
                </span>
              `
            : ""}
        </div>
      </footer>
    `;
  }

  private renderPage() {
    switch (this.viewState.route) {
      case "signUp": {
        if (!this.isAppSettingsLoaded) {
          return html`<div
            class="flex w-full items-center justify-center md:bg-neutral-50"
          ></div>`;
        }
        if (this.isRegistrationEnabled) {
          return html`<btrix-sign-up
            class="flex w-full items-center justify-center md:bg-neutral-50"
            .authState="${this.authService.authState}"
          ></btrix-sign-up>`;
        } else {
          return this.renderNotFoundPage();
        }
      }

      case "verify":
        return html`<btrix-verify
          class="flex w-full items-center justify-center md:bg-neutral-50"
          token="${this.viewState.params.token}"
          @user-info-change="${this.onUserInfoChange}"
          .authState="${this.authService.authState}"
        ></btrix-verify>`;

      case "join":
        return html`<btrix-join
          class="flex w-full items-center justify-center md:bg-neutral-50"
          token="${this.viewState.params.token}"
          email="${this.viewState.params.email}"
        ></btrix-join>`;

      case "acceptInvite":
        return html`<btrix-accept-invite
          class="flex w-full items-center justify-center md:bg-neutral-50"
          .authState="${this.authService.authState}"
          token="${this.viewState.params.token}"
          email="${this.viewState.params.email}"
        ></btrix-accept-invite>`;

      case "login":
      case "loginWithRedirect":
      case "forgotPassword":
        return html`<btrix-log-in
          class="flex w-full items-center justify-center md:bg-neutral-50"
          @navigate=${this.onNavigateTo}
          .viewState=${this.viewState}
          redirectUrl=${this.viewState.params.redirectUrl ||
          this.viewState.data?.redirectUrl}
        ></btrix-log-in>`;

      case "resetPassword":
        return html`<btrix-reset-password
          class="flex w-full items-center justify-center md:bg-neutral-50"
          @navigate=${this.onNavigateTo}
          .viewState=${this.viewState}
        ></btrix-reset-password>`;

      case "home":
        return html`<btrix-home
          class="w-full md:bg-neutral-50"
          @navigate=${this.onNavigateTo}
          @update-user-info=${(e: CustomEvent) => {
            e.stopPropagation();
            void this.updateUserInfo();
          }}
          .authState=${this.authService.authState}
          .userInfo=${this.appState.userInfo ?? undefined}
          slug=${ifDefined(this.appState.orgSlug ?? undefined)}
        ></btrix-home>`;

      case "orgs":
        return html`<btrix-orgs
          class="w-full md:bg-neutral-50"
          .authState="${this.authService.authState}"
          .userInfo="${this.appState.userInfo ?? undefined}"
        ></btrix-orgs>`;

      case "org": {
        const slug = this.viewState.params.slug;
        const orgPath = this.viewState.pathname;
        const orgTab =
          window.location.pathname
            .slice(window.location.pathname.indexOf(slug) + slug.length)
            .replace(/(^\/|\/$)/, "")
            .split("/")[0] || "home";
        return html`<btrix-org
          class="w-full"
          @navigate=${this.onNavigateTo}
          @update-user-info=${(e: CustomEvent) => {
            e.stopPropagation();
            void this.updateUserInfo();
          }}
          .authState=${this.authService.authState}
          .userInfo=${this.appState.userInfo ?? undefined}
          .viewStateData=${this.viewState.data}
          .params=${this.viewState.params}
          slug=${slug}
          orgPath=${orgPath.split(slug)[1]}
          orgTab=${orgTab as OrgTab}
        ></btrix-org>`;
      }

      case "accountSettings":
        return html`<btrix-account-settings
          class="mx-auto box-border w-full max-w-screen-desktop p-2 md:py-8"
          @update-user-info=${(e: CustomEvent) => {
            e.stopPropagation();
            void this.updateUserInfo();
          }}
          .authState="${this.authService.authState}"
          .userInfo="${this.appState.userInfo ?? undefined}"
        ></btrix-account-settings>`;

      case "usersInvite": {
        if (this.appState.userInfo) {
          if (this.appState.userInfo.isAdmin) {
            return html`<btrix-users-invite
              class="mx-auto box-border w-full max-w-screen-desktop p-2 md:py-8"
              .authState="${this.authService.authState}"
              .userInfo="${this.appState.userInfo}"
            ></btrix-users-invite>`;
          } else {
            return this.renderNotFoundPage();
          }
        } else {
          return this.renderSpinner();
        }
      }

      case "crawls":
      case "crawl": {
        if (this.appState.userInfo) {
          if (this.appState.userInfo.isAdmin) {
            return html`<btrix-crawls
              class="w-full"
              @navigate=${this.onNavigateTo}
              @notify=${this.onNotify}
              .authState=${this.authService.authState}
              crawlId=${this.viewState.params.crawlId}
            ></btrix-crawls>`;
          } else {
            return this.renderNotFoundPage();
          }
        } else {
          return this.renderSpinner();
        }
      }

      case "awpUploadRedirect": {
        const { orgId, uploadId } = this.viewState.params;
        if (this.appState.slugLookup) {
          const slug = this.appState.slugLookup[orgId];
          if (slug) {
            this.navigate(`/orgs/${slug}/items/upload/${uploadId}`);
            return;
          }
        }
        // falls through
      }

      default:
        return this.renderNotFoundPage();
    }
  }

  private renderSpinner() {
    return html`
      <div class="flex w-full items-center justify-center text-3xl">
        <sl-spinner></sl-spinner>
      </div>
    `;
  }

  private renderNotFoundPage() {
    return html`<btrix-not-found
      class="flex w-full items-center justify-center md:bg-neutral-50"
    ></btrix-not-found>`;
  }

  private renderFindCrawl() {
    return html`
      <sl-dropdown
        @sl-after-show=${(e: Event) => {
          (e.target as HTMLElement).querySelector("sl-input")?.focus();
        }}
        @sl-after-hide=${(e: Event) => {
          (e.target as HTMLElement).querySelector("sl-input")!.value = "";
        }}
        hoist
      >
        <button
          slot="trigger"
          class="font-medium text-primary hover:text-indigo-400"
        >
          ${msg("Jump to Crawl")}
        </button>

        <div class="p-2">
          <form
            @submit=${(e: SubmitEvent) => {
              e.preventDefault();
              const id = new FormData(e.target as HTMLFormElement).get(
                "crawlId",
              ) as string;
              this.navigate(`/crawls/crawl/${id}#watch`);
              void (e.target as HTMLFormElement).closest("sl-dropdown")?.hide();
            }}
          >
            <div class="flex flex-wrap items-center">
              <div class="w-90 mr-2">
                <sl-input
                  size="small"
                  name="crawlId"
                  placeholder=${msg("Enter Crawl ID")}
                  required
                ></sl-input>
              </div>
              <div class="grow-0">
                <sl-button size="small" variant="neutral" type="submit">
                  <sl-icon slot="prefix" name="arrow-right-circle"></sl-icon>
                  ${msg("Go")}</sl-button
                >
              </div>
            </div>
          </form>
        </div>
      </sl-dropdown>
    `;
  }

  onLogOut(event: CustomEvent<{ redirect?: boolean } | null>) {
    const detail = event.detail || {};
    const redirect = detail.redirect !== false;

    this.clearUser();

    if (redirect) {
      this.navigate(ROUTES.login);
    }
  }

  onLoggedIn(event: CustomEvent<LoggedInEventDetail>) {
    const { detail } = event;

    this.authService.saveLogin({
      username: detail.username,
      headers: detail.headers,
      tokenExpiresAt: detail.tokenExpiresAt,
    });

    if (!detail.api) {
      this.navigate(detail.redirectUrl || ROUTES.home);
    }

    if (detail.firstLogin) {
      this.onFirstLogin({ email: detail.username });
    }

    void this.updateUserInfo();
  }

  onNeedLogin = (e: CustomEvent<NeedLoginEventDetail>) => {
    e.stopPropagation();

    this.clearUser();
    const redirectUrl = e.detail?.redirectUrl;
    this.navigate(ROUTES.login, {
      redirectUrl,
    });
    this.notify({
      message: msg("Please log in to continue."),
      variant: "warning",
      icon: "exclamation-triangle",
    });
  };

  onNavigateTo = (event: CustomEvent<NavigateEventDetail>) => {
    event.stopPropagation();

    this.navigate(event.detail.url, event.detail.state);

    // Scroll to top of page
    window.scrollTo({ top: 0 });
  };

  onUserInfoChange(event: CustomEvent<Partial<CurrentUser>>) {
    AppStateService.updateUserInfo({
      ...this.appState.userInfo,
      ...event.detail,
    } as CurrentUser);
  }

  /**
   * Show global toast alert
   */
  onNotify = (event: CustomEvent<NotifyEventDetail>) => {
    event.stopPropagation();

    const {
      title,
      message,
      variant = "primary",
      icon = "info-circle",
      duration = 5000,
    } = event.detail;

    const container = document.createElement("sl-alert");
    const alert = Object.assign(container, {
      variant,
      closable: true,
      duration: duration,
      style: [
        "--sl-panel-background-color: var(--sl-color-neutral-1000)",
        "--sl-color-neutral-700: var(--sl-color-neutral-0)",
        // "--sl-panel-border-width: 0px",
        "--sl-spacing-large: var(--sl-spacing-medium)",
      ].join(";"),
    });

    render(
      html`
        <sl-icon name="${icon}" slot="icon"></sl-icon>
        ${title ? html`<strong>${title}</strong>` : ""}
        ${message ? html`<div>${message}</div>` : ""}
      `,
      container,
    );
    document.body.append(alert);
    void alert.toast();
  };

  getUserInfo(): Promise<APIUser> {
    return this.apiFetch("/users/me", this.authService.authState!);
  }

  private clearUser() {
    this.authService.logout();
    this.authService = new AuthService();
    AppStateService.reset();
  }

  private showDialog(content: DialogContent) {
    this.globalDialogContent = content;
    void this.globalDialog.show();
  }

  private closeDialog() {
    void this.globalDialog.hide();
  }

  private onFirstLogin({ email }: { email: string }) {
    this.showDialog({
      label: "Welcome to Browsertrix Cloud",
      noHeader: true,
      body: html`
        <div class="grid gap-4 text-center">
          <p class="mt-8 text-xl font-medium">
            ${msg("Welcome to Browsertrix Cloud!")}
          </p>

          <p>
            ${msg(
              html`A confirmation email was sent to: <br />
                <strong>${email}</strong>.`,
            )}
          </p>
          <p class="mx-auto max-w-xs">
            ${msg(
              "Click the link in your email to confirm your email address.",
            )}
          </p>
        </div>

        <div class="mb-4 mt-8 text-center">
          <sl-button variant="primary" @click=${() => this.closeDialog()}
            >${msg("Got it, go to dashboard")}</sl-button
          >
        </div>
      `,
    });
  }

  private startSyncBrowserTabs() {
    AuthService.broadcastChannel.addEventListener(
      "message",
      ({ data }: { data: AuthStorageEventDetail }) => {
        if (data.name === "auth_storage") {
          if (data.value !== AuthService.storage.getItem()) {
            if (data.value) {
              this.authService.saveLogin(JSON.parse(data.value) as Auth);
              void this.updateUserInfo();
              this.syncViewState();
            } else {
              this.clearUser();
              this.navigate(ROUTES.login);
            }
          }
        }
      },
    );
  }

  private clearSelectedOrg() {
    AppStateService.updateOrgSlug(null);
  }
}
