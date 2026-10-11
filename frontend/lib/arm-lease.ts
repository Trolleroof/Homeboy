const store = globalThis as unknown as { homeboyArmLease?: { held: boolean } };
export const armLease = store.homeboyArmLease ??= { held: false };
