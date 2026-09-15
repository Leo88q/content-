use anchor_lang::prelude::*;

#[error_code]
pub enum SixsecError {
    #[msg("Недостаточно свободных средств в пуле призов под резерв задания")]
    InsufficientPoolReserve,
    #[msg("В пуле не хватает фактического баланса для выплаты")]
    PoolBalanceShort,
    #[msg("Mint награды имеет активный freeze authority — выдача может быть заблокирована третьей стороной")]
    RewardMintFreezable,
    #[msg("Переполнение при расчёте резерва")]
    ReserveOverflow,
    #[msg("tier_id вне диапазона объявленных тиров")]
    TierOutOfRange,
    #[msg("Задание уже набрало max_claims воркеров")]
    NoClaimsLeft,
    #[msg("Срок claim истёк")]
    ClaimExpired,
    #[msg("Срок задания истёк")]
    TaskExpired,
    #[msg("Срок задания ещё не истёк — возврат невозможен")]
    TaskNotExpired,
    #[msg("Вывод из пула затронул бы зарезервированные под открытые задания средства")]
    WithdrawWouldBreakReserves,
    #[msg("Недопустимая длина строкового поля")]
    FieldTooLong,
    #[msg("Задание должно объявлять хотя бы один тир")]
    NoTiers,
    #[msg("NFT-предмет тира отсутствует в пуле")]
    NftNotInPool,
    #[msg("Подписант не является авторитетом модерации")]
    UnauthorizedModerator,
    #[msg("Сабмишен уже рассмотрен — повторное решение невозможно")]
    AlreadyModerated,
    #[msg("Сабмишен не одобрен — выплата невозможна")]
    NotApproved,
    #[msg("Профиль воркера не соответствует автору сабмишена")]
    ProfileWorkerMismatch,
    #[msg("Переданный mint не совпадает с SKR-минтом, заданным при инициализации пула")]
    SkrMintMismatch,
    #[msg("Ранг воркера ниже порога SKR-бонуса")]
    NoRankBonus,
}
