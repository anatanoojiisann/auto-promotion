// Receipts are durable before submission. An existing receipt is never submitted again.
export class ExecutorCore {
  constructor({
    store,
    authorize,
    submit,
    classifyError = () => ({ reason: "NativeSubmissionUnconfirmed" }),
    clock = () => Date.now() / 1000,
  }) {
    this.store = store;
    this.authorize = authorize;
    this.submit = submit;
    this.clock = clock;
    this.classifyError = classifyError;
  }
  async execute(request) {
    if (!request?.job_id || !request.attempt_id || !request.lease_token)
      throw new Error("NativeLeaseRequired");
    const key = "receipt:" + request.job_id,
      old = await this.store.get(key);
    if (old) {
      if (
        old.job_version !== request.version ||
        old.account_id !== request.account_id ||
        old.attempt_id !== request.attempt_id
      )
        throw new Error("FrozenExecutorReceiptChanged");
      return { ...old, replayed: false, from_durable_receipt: true };
    }
    const gate = await this.authorize(request);
    if (
      !gate?.allowed ||
      !gate.job ||
      gate.job.id !== request.job_id ||
      gate.job.version !== request.version ||
      gate.job.account_id !== request.account_id ||
      gate.lease_until <= this.clock()
    )
      throw new Error("NativeQueueAuthorizationRequired");
    let latest = {
      status: "unknown",
      verified: false,
      attempt_id: request.attempt_id,
      job_version: request.version,
      account_id: request.account_id,
      submission_started: false,
    };
    await this.store.put(key, latest);
    const checkpoint = async (value) => {
      latest = { ...latest, ...value };
      await this.store.put(key, latest);
    };
    const revalidate = async () => {
      const current = await this.authorize(request);
      if (
        !current?.allowed ||
        current.lease_until <= this.clock() ||
        current.job?.version !== gate.job.version
      )
        throw new Error("NativeQueueAuthorizationExpired");
    };
    try {
      const result = await this.submit(gate.job, checkpoint, revalidate);
      await checkpoint(result);
      return latest;
    } catch (error) {
      await checkpoint(this.classifyError(error));
      return latest;
    }
  }
}
