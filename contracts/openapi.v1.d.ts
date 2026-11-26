export interface paths {
    "/health/live": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Live */
        get: operations["live_health_live_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/health/ready": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Ready */
        get: operations["ready_health_ready_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/admin/mail/outbound/{outbound_id}/reconcile": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Reconcile */
        post: operations["reconcile_v1_admin_mail_outbound__outbound_id__reconcile_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/admin/mail/status": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Status */
        get: operations["status_v1_admin_mail_status_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/conversations/{conversation_id}/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Events */
        get: operations["events_v1_conversations__conversation_id__events_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/conversations/{conversation_id}/messages": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** History */
        get: operations["history_v1_conversations__conversation_id__messages_get"];
        put?: never;
        /** Admit */
        post: operations["admit_v1_conversations__conversation_id__messages_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/mail/requests/{request_id}/followup": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Followup */
        post: operations["followup_v1_mail_requests__request_id__followup_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/offers/{offer_id}/confirm": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Confirm */
        post: operations["confirm_v1_offers__offer_id__confirm_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/offers/{offer_id}/decline": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Decline */
        post: operations["decline_v1_offers__offer_id__decline_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/ops/deliveries": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Deliveries */
        get: operations["deliveries_v1_ops_deliveries_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/ops/dion/delivery-attempts/{attempt_id}/reconcile": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Reconcile Dion Delivery */
        post: operations["reconcile_dion_delivery_v1_ops_dion_delivery_attempts__attempt_id__reconcile_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/ops/documents/{source_key}/sync": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Request Sync */
        post: operations["request_sync_v1_ops_documents__source_key__sync_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/ops/documents/sync/{run_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Sync Result */
        get: operations["sync_result_v1_ops_documents_sync__run_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/ops/integrations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Integration States */
        get: operations["integration_states_v1_ops_integrations_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/ops/jobs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Jobs */
        get: operations["jobs_v1_ops_jobs_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/ops/latencies": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Latencies */
        get: operations["latencies_v1_ops_latencies_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/ops/model-calls": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Model Calls */
        get: operations["model_calls_v1_ops_model_calls_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/ops/model-costs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Cost Report */
        get: operations["cost_report_v1_ops_model_costs_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/quality/conflicts": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Conflicts */
        get: operations["conflicts_v1_quality_conflicts_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/quality/faq/{faq_id}/coverage": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Cover */
        post: operations["cover_v1_quality_faq__faq_id__coverage_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/quality/faq/{faq_id}/replace": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Replace */
        post: operations["replace_v1_quality_faq__faq_id__replace_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/quality/faq/{faq_id}/withdraw": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Withdraw */
        post: operations["withdraw_v1_quality_faq__faq_id__withdraw_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/quality/inbound/{inbound_id}/bind": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Bind */
        post: operations["bind_v1_quality_inbound__inbound_id__bind_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/quality/inbound/{inbound_id}/raw": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Read Raw */
        get: operations["read_raw_v1_quality_inbound__inbound_id__raw_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/quality/requests": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Quality Queue */
        get: operations["quality_queue_v1_quality_requests_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/sources/{revision_id}/fragments/{fragment_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Source */
        get: operations["get_source_v1_sources__revision_id__fragments__fragment_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/turns/{turn_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Turn */
        get: operations["get_turn_v1_turns__turn_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/v1/turns/{turn_id}/retry": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Retry */
        post: operations["retry_v1_turns__turn_id__retry_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /** AcceptedMessage */
        AcceptedMessage: {
            /**
             * Case Id
             * Format: uuid
             */
            case_id: string;
            /**
             * Conversation Id
             * Format: uuid
             */
            conversation_id: string;
            /**
             * Turn Id
             * Format: uuid
             */
            turn_id: string;
        };
        /** AuthenticationError */
        AuthenticationError: {
            /** Detail */
            detail: string;
        };
        /** ClarificationOptionView */
        ClarificationOptionView: {
            /** Conditions */
            conditions?: {
                [key: string]: string;
            };
            /** Id */
            id: string;
            /** Text */
            text: string;
        };
        /** ClarificationView */
        ClarificationView: {
            /**
             * Case Id
             * Format: uuid
             */
            case_id: string;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /** Options */
            options: components["schemas"]["ClarificationOptionView"][];
            /** Prompt */
            prompt: string;
            /**
             * Status
             * @enum {string}
             */
            status: "open" | "resolved" | "superseded";
            /**
             * Turn Id
             * Format: uuid
             */
            turn_id: string;
            /** Version */
            version: number;
        };
        /** CommandValidationError */
        CommandValidationError: {
            /** Detail */
            detail: components["schemas"]["DialogueErrorDetail"] | components["schemas"]["ValidationIssue"][];
        };
        /** CoverFaqBody */
        CoverFaqBody: {
            /**
             * Revision Id
             * Format: uuid
             */
            revision_id: string;
            /** Version */
            version: number;
        };
        /** CursorSummary */
        CursorSummary: {
            /** Last Uid */
            last_uid: number;
            /** Uidvalidity */
            uidvalidity: string;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
        };
        /** DeliveryAttempt */
        DeliveryAttempt: {
            /**
             * Channel
             * @enum {string}
             */
            channel: "smtp" | "dion";
            /** Detail */
            detail: string | null;
            /** Effect Key */
            effect_key: string;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /** Job Id */
            job_id: string | null;
            /** Provider Id */
            provider_id: string | null;
            /**
             * Started At
             * Format: date-time
             */
            started_at: string;
            /**
             * State
             * @enum {string}
             */
            state: "sending" | "sent" | "failed" | "delivery_unknown";
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
        };
        /** DeliveryAttempts */
        DeliveryAttempts: {
            /** Deliveries */
            deliveries: components["schemas"]["DeliveryAttempt"][];
        };
        /** DialogueErrorDetail */
        DialogueErrorDetail: {
            /** Code */
            code: string;
            /** Cursor */
            cursor?: number | null;
        } & {
            [key: string]: unknown;
        };
        /** DialogueErrorResponse */
        DialogueErrorResponse: {
            detail: components["schemas"]["DialogueErrorDetail"];
        };
        /** DionDeliveryDecision */
        DionDeliveryDecision: {
            /** Evidence */
            evidence: string;
            /** Message Id */
            message_id?: string | null;
            /**
             * Retry
             * @default false
             */
            retry: boolean;
            /**
             * State
             * @enum {string}
             */
            state: "sent" | "failed";
        };
        /** DionDeliveryDecisionResult */
        DionDeliveryDecisionResult: {
            /**
             * Attempt Id
             * Format: uuid
             */
            attempt_id: string;
            /** Retry Job Id */
            retry_job_id: string | null;
            /**
             * State
             * @enum {string}
             */
            state: "sent" | "failed";
        };
        /** EventCursorExpired */
        EventCursorExpired: {
            /**
             * Code
             * @default event_cursor_expired
             * @constant
             */
            code: "event_cursor_expired";
            /** Cursor */
            cursor: number;
        };
        /** EventCursorExpiredResponse */
        EventCursorExpiredResponse: {
            detail: components["schemas"]["EventCursorExpired"];
        };
        /** EventView */
        EventView: {
            /**
             * Case Id
             * Format: uuid
             */
            case_id: string;
            /**
             * Conversation Id
             * Format: uuid
             */
            conversation_id: string;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Cursor */
            cursor: number;
            /** Kind */
            kind: string;
            /** Payload */
            payload: {
                [key: string]: unknown;
            };
            /** Turn Id */
            turn_id: string | null;
        };
        /** FaqConflict */
        FaqConflict: {
            /** Conflict Revision Id */
            conflict_revision_id: string | null;
            /** Covered Revision Id */
            covered_revision_id: string | null;
            /**
             * Current Revision Id
             * Format: uuid
             */
            current_revision_id: string;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /**
             * Priority
             * @default highest
             * @constant
             */
            priority: "highest";
            /** Status */
            status: string;
            /** Version */
            version: number;
        };
        /** FaqSummary */
        FaqSummary: {
            /** Conflict Revision Id */
            conflict_revision_id: string | null;
            /** Covered Revision Id */
            covered_revision_id: string | null;
            /**
             * Current Revision Id
             * Format: uuid
             */
            current_revision_id: string;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /** Status */
            status: string;
            /** Version */
            version: number;
        };
        /** FaqVersion */
        FaqVersion: {
            /** Version */
            version: number;
        };
        /** FollowupBody */
        FollowupBody: {
            /** Client Id */
            client_id: string;
            /** Text */
            text: string;
        };
        /** HistoryView */
        HistoryView: {
            /**
             * Conversation Id
             * Format: uuid
             */
            conversation_id: string;
            /** Cursor */
            cursor: number;
            /** Mail Followups */
            mail_followups?: components["schemas"]["MailFollowupView"][];
            /** Messages */
            messages: components["schemas"]["MessageView"][];
            /** Turns */
            turns: components["schemas"]["TurnView"][];
        };
        /** HTTPValidationError */
        HTTPValidationError: {
            /** Detail */
            detail?: components["schemas"]["ValidationError"][];
        };
        /** InboundBinding */
        InboundBinding: {
            /**
             * Request Id
             * Format: uuid
             */
            request_id: string;
        };
        /** InboundSummary */
        InboundSummary: {
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /** Message Id */
            message_id: string | null;
            /** Reason */
            reason: string | null;
            /** Request Id */
            request_id: string | null;
            /** State */
            state: string;
            /** Trusted */
            trusted: boolean;
        };
        /** IntegrationStates */
        IntegrationStates: {
            /** Integrations */
            integrations: {
                [key: string]: unknown;
            };
        };
        /** JobStatus */
        JobStatus: {
            /** Attempt */
            attempt: number;
            /**
             * Available At
             * Format: date-time
             */
            available_at: string;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Deadline At */
            deadline_at: string | null;
            /** Finished At */
            finished_at: string | null;
            /** First Started At */
            first_started_at: string | null;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /** Kind */
            kind: string;
            /** Last Error */
            last_error: string | null;
            /** Max Attempts */
            max_attempts: number;
            /**
             * Queue
             * @enum {string}
             */
            queue: "interactive" | "background";
            /** Queue Wait Seconds */
            queue_wait_seconds: number | null;
            /**
             * State
             * @enum {string}
             */
            state: "queued" | "running" | "succeeded" | "failed";
        };
        /** JobStatuses */
        JobStatuses: {
            /** Jobs */
            jobs: components["schemas"]["JobStatus"][];
        };
        /** Liveness */
        Liveness: {
            /**
             * Status
             * @constant
             */
            status: "ok";
        };
        /** MailFollowupView */
        MailFollowupView: {
            /**
             * Case Id
             * Format: uuid
             */
            case_id: string;
            /** Pending */
            pending: string[];
            /**
             * Request Id
             * Format: uuid
             */
            request_id: string;
            /**
             * State
             * @enum {string}
             */
            state: "counter_question" | "partial";
            /**
             * Turn Id
             * Format: uuid
             */
            turn_id: string;
        };
        /** MailStatus */
        MailStatus: {
            cursor: components["schemas"]["CursorSummary"] | null;
            /**
             * Mode
             * @enum {string}
             */
            mode: "disabled" | "simulator" | "network";
            /** Outbound */
            outbound: components["schemas"]["OutboundSummary"][];
        };
        /** MessageRequest */
        MessageRequest: {
            /** Case Id */
            case_id?: string | null;
            /** Clarification Id */
            clarification_id?: string | null;
            /** Clarification Version */
            clarification_version?: number | null;
            /** Client Message Id */
            client_message_id: string;
            /** Option Id */
            option_id?: string | null;
            /** Text */
            text?: string | null;
        };
        /** MessageView */
        MessageView: {
            /**
             * Case Id
             * Format: uuid
             */
            case_id: string;
            /** Client Message Id */
            client_message_id?: string | null;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /**
             * Role
             * @enum {string}
             */
            role: "user" | "assistant" | "system";
            /** Source Refs */
            source_refs?: components["schemas"]["SourceReference"][] | null;
            /** Text */
            text: string;
            /** Turn Id */
            turn_id?: string | null;
        };
        /** ModelCall */
        ModelCall: {
            /** Case Id */
            case_id: string | null;
            /** Cost */
            cost: string | null;
            /** Detail */
            detail: string | null;
            /** Finished At */
            finished_at: string | null;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /** Input Tokens */
            input_tokens: number | null;
            /** Job Id */
            job_id: string | null;
            /** Model */
            model: string;
            /** Output Tokens */
            output_tokens: number | null;
            /** Role */
            role: string;
            /** Route */
            route: string;
            /**
             * Started At
             * Format: date-time
             */
            started_at: string;
            /**
             * State
             * @enum {string}
             */
            state: "calling" | "succeeded" | "failed" | "outcome_unknown";
            /** Trace Id */
            trace_id: string;
            /** Turn Id */
            turn_id: string | null;
        };
        /** ModelCalls */
        ModelCalls: {
            /** Calls */
            calls: components["schemas"]["ModelCall"][];
        };
        /** ModelCost */
        ModelCost: {
            /** Calls */
            calls: number;
            /** Failed Calls */
            failed_calls: number;
            /** Input Tokens */
            input_tokens: number | null;
            /** Known Cost */
            known_cost: string | null;
            /** Output Tokens */
            output_tokens: number | null;
            /**
             * Scope
             * @enum {string}
             */
            scope: "turn" | "case" | "background";
            /** Scope Id */
            scope_id: string | null;
            /** Unknown Cost Calls */
            unknown_cost_calls: number;
            /** Unresolved Calls */
            unresolved_calls: number;
        };
        /** ModelCosts */
        ModelCosts: {
            /** Costs */
            costs: components["schemas"]["ModelCost"][];
        };
        /** OfferDecision */
        OfferDecision: {
            /** Version */
            version: number;
        };
        /** OfferView */
        OfferView: {
            /**
             * Case Id
             * Format: uuid
             */
            case_id: string;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /**
             * Status
             * @enum {string}
             */
            status: "open" | "confirmed" | "declined" | "superseded";
            /** Text */
            text: string;
            /**
             * Turn Id
             * Format: uuid
             */
            turn_id: string;
            /** Version */
            version: number;
        };
        /** OutboundAccepted */
        OutboundAccepted: {
            /**
             * Outbound Id
             * Format: uuid
             */
            outbound_id: string;
        };
        /** OutboundSummary */
        OutboundSummary: {
            /** Attempt Id */
            attempt_id: string | null;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /** Reason */
            reason: string | null;
            /**
             * Request Id
             * Format: uuid
             */
            request_id: string;
            /** State */
            state: string;
        };
        /** QualityQueue */
        QualityQueue: {
            /** Inbound */
            inbound: components["schemas"]["InboundSummary"][];
            /** Requests */
            requests: components["schemas"]["RequestSummary"][];
        };
        /** Readiness */
        Readiness: {
            /**
             * Database
             * @enum {string}
             */
            database: "available" | "unavailable";
            /**
             * Status
             * @enum {string}
             */
            status: "ready" | "unavailable";
        };
        /** ReconciliationBody */
        ReconciliationBody: {
            /**
             * Attempt Id
             * Format: uuid
             */
            attempt_id: string;
            /** Detail */
            detail: string;
            /**
             * Retry
             * @default false
             */
            retry: boolean;
            /**
             * State
             * @enum {string}
             */
            state: "sent" | "failed";
        };
        /** ReconciliationResult */
        ReconciliationResult: {
            /**
             * Attempt Id
             * Format: uuid
             */
            attempt_id: string;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /** State */
            state: string;
        };
        /** ReplaceFaqBody */
        ReplaceFaqBody: {
            /** Text */
            text: string;
            /** Version */
            version: number;
        };
        /** Replacement */
        Replacement: {
            /** Conflict Revision Id */
            conflict_revision_id: string | null;
            /**
             * Fragment Id
             * Format: uuid
             */
            fragment_id: string;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /**
             * Revision Id
             * Format: uuid
             */
            revision_id: string;
            /** Version */
            version: number;
        };
        /** RequestSummary */
        RequestSummary: {
            /**
             * Case Id
             * Format: uuid
             */
            case_id: string;
            /**
             * Conversation Id
             * Format: uuid
             */
            conversation_id: string;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /** Pending */
            pending: string[];
            /** State */
            state: string;
            /**
             * Turn Id
             * Format: uuid
             */
            turn_id: string;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
        };
        /** ResultView */
        ResultView: {
            /**
             * Action
             * @enum {string}
             */
            action: "answer" | "clarify" | "escalate" | "out_of_scope";
            /** Notice */
            notice?: string | null;
            /** Sources */
            sources?: components["schemas"]["SourceReference"][];
            /** Text */
            text: string;
        };
        /** SourceReference */
        SourceReference: {
            /** Claim */
            claim?: string | null;
            /**
             * Document Id
             * Format: uuid
             */
            document_id: string;
            /**
             * Fragment Id
             * Format: uuid
             */
            fragment_id: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "document" | "faq";
            /** Quote */
            quote: string;
            /**
             * Revision Id
             * Format: uuid
             */
            revision_id: string;
            /** Url */
            url: string;
        };
        /** SourceView */
        SourceView: {
            /** Anchor */
            anchor: string;
            /**
             * Document Id
             * Format: uuid
             */
            document_id: string;
            /**
             * Fragment Id
             * Format: uuid
             */
            fragment_id: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "document" | "faq";
            /**
             * Revision Id
             * Format: uuid
             */
            revision_id: string;
            /** Text */
            text: string;
            /** Title */
            title: string;
        };
        /** SyncAccepted */
        SyncAccepted: {
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
        };
        /** SyncResult */
        SyncResult: {
            /** Batch Offset */
            batch_offset: number;
            /** Counts */
            counts: {
                [key: string]: number;
            };
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Cursor */
            cursor: string | null;
            /** Error */
            error: string | null;
            /** Finished At */
            finished_at: string | null;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /** Listing Done */
            listing_done: boolean;
            /** Source Key */
            source_key: string;
            /** Started At */
            started_at: string | null;
            /**
             * Status
             * @enum {string}
             */
            status: "queued" | "running" | "incomplete" | "completed";
        };
        /** TurnLatencies */
        TurnLatencies: {
            /** Latencies */
            latencies: components["schemas"]["TurnLatency"][];
        };
        /** TurnLatency */
        TurnLatency: {
            /**
             * Case Id
             * Format: uuid
             */
            case_id: string;
            /** First Useful Seconds */
            first_useful_seconds: number | null;
            /** Human Wait Seconds */
            human_wait_seconds: number | null;
            /**
             * Turn Id
             * Format: uuid
             */
            turn_id: string;
            /** Verified Answer Seconds */
            verified_answer_seconds: number | null;
        };
        /** TurnView */
        TurnView: {
            /** Attempt */
            attempt: number;
            /**
             * Case Id
             * Format: uuid
             */
            case_id: string;
            /** Clarifications */
            clarifications: components["schemas"]["ClarificationView"][];
            /**
             * Conversation Id
             * Format: uuid
             */
            conversation_id: string;
            /** Cursor */
            cursor: number;
            /** Error */
            error: string | null;
            /** Mail Followups */
            mail_followups?: components["schemas"]["MailFollowupView"][];
            /** Offers */
            offers: components["schemas"]["OfferView"][];
            /** Question */
            question: string;
            result: components["schemas"]["ResultView"] | null;
            /**
             * Status
             * @enum {string}
             */
            status: "queued" | "processing" | "completed" | "technical_failure";
            /**
             * Turn Id
             * Format: uuid
             */
            turn_id: string;
        };
        /** ValidationError */
        ValidationError: {
            /** Context */
            ctx?: Record<string, never>;
            /** Input */
            input?: unknown;
            /** Location */
            loc: (string | number)[];
            /** Message */
            msg: string;
            /** Error Type */
            type: string;
        };
        /** ValidationIssue */
        ValidationIssue: {
            /** Ctx */
            ctx?: {
                [key: string]: unknown;
            } | null;
            /** Input */
            input?: unknown;
            /** Loc */
            loc: (string | number)[];
            /** Msg */
            msg: string;
            /** Type */
            type: string;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    live_health_live_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Liveness"];
                };
            };
        };
    };
    ready_health_ready_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Readiness"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Readiness"];
                };
            };
        };
    };
    reconcile_v1_admin_mail_outbound__outbound_id__reconcile_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                outbound_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ReconciliationBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ReconciliationResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    status_v1_admin_mail_status_get: {
        parameters: {
            query?: {
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MailStatus"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    events_v1_conversations__conversation_id__events_get: {
        parameters: {
            query?: {
                after?: number | null;
            };
            header?: {
                "Last-Event-ID"?: string | null;
            };
            path: {
                conversation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Server-sent events. Resume with Last-Event-ID from the same conversation. After a 410 response, read the conversation snapshot and resume at its cursor. */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "text/event-stream": string;
                };
            };
            /** @description A valid bearer token is required. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AuthenticationError"];
                };
            };
            /** @description This operation requires the employee role. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The resource does not exist or is unavailable. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The command conflicts with current state. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description Cursor expired. Read a new conversation snapshot. */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EventCursorExpiredResponse"];
                };
            };
            /** @description The command or request body is invalid. */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CommandValidationError"];
                };
            };
        };
    };
    history_v1_conversations__conversation_id__messages_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HistoryView"];
                };
            };
            /** @description A valid bearer token is required. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AuthenticationError"];
                };
            };
            /** @description This operation requires the employee role. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The resource does not exist or is unavailable. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The command or request body is invalid. */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CommandValidationError"];
                };
            };
        };
    };
    admit_v1_conversations__conversation_id__messages_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                conversation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MessageRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AcceptedMessage"];
                };
            };
            /** @description A valid bearer token is required. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AuthenticationError"];
                };
            };
            /** @description This operation requires the employee role. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The resource does not exist or is unavailable. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The command conflicts with current state. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The command or request body is invalid. */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CommandValidationError"];
                };
            };
            /** @description The interactive queue cannot accept the command. */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
        };
    };
    followup_v1_mail_requests__request_id__followup_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                request_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FollowupBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OutboundAccepted"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    confirm_v1_offers__offer_id__confirm_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                offer_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["OfferDecision"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OfferView"];
                };
            };
            /** @description A valid bearer token is required. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AuthenticationError"];
                };
            };
            /** @description This operation requires the employee role. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The resource does not exist or is unavailable. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The command conflicts with current state. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The command or request body is invalid. */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CommandValidationError"];
                };
            };
            /** @description The interactive queue cannot accept the command. */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
        };
    };
    decline_v1_offers__offer_id__decline_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                offer_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["OfferDecision"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OfferView"];
                };
            };
            /** @description A valid bearer token is required. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AuthenticationError"];
                };
            };
            /** @description This operation requires the employee role. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The resource does not exist or is unavailable. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The command conflicts with current state. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The command or request body is invalid. */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CommandValidationError"];
                };
            };
        };
    };
    deliveries_v1_ops_deliveries_get: {
        parameters: {
            query?: {
                limit?: number;
                state?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DeliveryAttempts"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    reconcile_dion_delivery_v1_ops_dion_delivery_attempts__attempt_id__reconcile_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                attempt_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DionDeliveryDecision"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DionDeliveryDecisionResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    request_sync_v1_ops_documents__source_key__sync_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                source_key: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SyncAccepted"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    sync_result_v1_ops_documents_sync__run_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SyncResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    integration_states_v1_ops_integrations_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["IntegrationStates"];
                };
            };
        };
    };
    jobs_v1_ops_jobs_get: {
        parameters: {
            query?: {
                limit?: number;
                queue?: string | null;
                state?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JobStatuses"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    latencies_v1_ops_latencies_get: {
        parameters: {
            query?: {
                case_id?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TurnLatencies"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    model_calls_v1_ops_model_calls_get: {
        parameters: {
            query?: {
                case_id?: string | null;
                limit?: number;
                turn_id?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ModelCalls"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    cost_report_v1_ops_model_costs_get: {
        parameters: {
            query?: {
                case_id?: string | null;
                turn_id?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ModelCosts"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    conflicts_v1_quality_conflicts_get: {
        parameters: {
            query?: {
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FaqConflict"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    cover_v1_quality_faq__faq_id__coverage_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                faq_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CoverFaqBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FaqSummary"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    replace_v1_quality_faq__faq_id__replace_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                faq_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ReplaceFaqBody"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Replacement"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    withdraw_v1_quality_faq__faq_id__withdraw_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                faq_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["FaqVersion"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FaqSummary"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    bind_v1_quality_inbound__inbound_id__bind_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                inbound_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InboundBinding"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["InboundSummary"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    read_raw_v1_quality_inbound__inbound_id__raw_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                inbound_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "message/rfc822": string;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    quality_queue_v1_quality_requests_get: {
        parameters: {
            query?: {
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["QualityQueue"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_source_v1_sources__revision_id__fragments__fragment_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                fragment_id: string;
                revision_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SourceView"];
                };
            };
            /** @description A valid bearer token is required. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AuthenticationError"];
                };
            };
            /** @description This operation requires the employee role. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The resource does not exist or is unavailable. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The command or request body is invalid. */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CommandValidationError"];
                };
            };
        };
    };
    get_turn_v1_turns__turn_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                turn_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TurnView"];
                };
            };
            /** @description A valid bearer token is required. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AuthenticationError"];
                };
            };
            /** @description This operation requires the employee role. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The resource does not exist or is unavailable. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The command or request body is invalid. */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CommandValidationError"];
                };
            };
        };
    };
    retry_v1_turns__turn_id__retry_post: {
        parameters: {
            query?: {
                /** @description Attempt shown on the failed turn snapshot. */
                attempt?: number | null;
            };
            header?: never;
            path: {
                turn_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AcceptedMessage"];
                };
            };
            /** @description A valid bearer token is required. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AuthenticationError"];
                };
            };
            /** @description This operation requires the employee role. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The resource does not exist or is unavailable. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The command conflicts with current state. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
            /** @description The command or request body is invalid. */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CommandValidationError"];
                };
            };
            /** @description The interactive queue cannot accept the command. */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DialogueErrorResponse"];
                };
            };
        };
    };
}

export type EventView = components["schemas"]["EventView"];
export type EventCursorExpired = components["schemas"]["EventCursorExpired"];
