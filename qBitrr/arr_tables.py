from peewee import BooleanField, DateTimeField, IntegerField, Model, TextField


class CommandsModel(Model):
    Id = IntegerField(null=False, primary_key=True)
    Name = TextField()
    Body = TextField()
    Priority = IntegerField()
    Status = IntegerField()
    QueuedAt = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    StartedAt = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    EndedAt = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    Duration = TextField()
    Exception = TextField()
    Trigger = IntegerField()
    Result = IntegerField()


class MoviesMetadataModel(Model):
    Id = IntegerField(null=False, primary_key=True)
    TmdbId = IntegerField()
    ImdbId = TextField()
    Images = TextField()
    Genres = TextField()
    Title = TextField()
    SortTitle = TextField()
    CleanTitle = TextField()
    OriginalTitle = TextField()
    CleanOriginalTitle = TextField()
    OriginalLanguage = IntegerField()
    Status = IntegerField()
    LastInfoSync = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    Runtime = IntegerField()
    InCinemas = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    PhysicalRelease = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    DigitalRelease = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    Year = IntegerField()
    SecondaryYear = IntegerField()
    Ratings = TextField()
    Recommendations = TextField()
    Certification = TextField()
    YouTubeTrailerId = TextField()
    Studio = TextField()
    Overview = TextField()
    Website = TextField()
    Popularity = IntegerField()
    CollectionTmdbId = IntegerField()
    CollectionTitle = TextField()


class MoviesModel(Model):
    Id = IntegerField(null=False, primary_key=True)
    Path = TextField()
    Monitored = IntegerField()
    ProfileId = IntegerField()
    Added = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    Tags = TextField()
    AddOptions = TextField()
    MovieFileId = IntegerField()
    MinimumAvailability = IntegerField()
    MovieMetadataId = IntegerField()


class MoviesModelv5(Model):
    Id = IntegerField(null=False, primary_key=True)
    Path = TextField()
    Monitored = IntegerField()
    QualityProfileId = IntegerField()
    Added = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    Tags = TextField()
    AddOptions = TextField()
    MovieFileId = IntegerField()
    MinimumAvailability = IntegerField()
    MovieMetadataId = IntegerField()


class EpisodesModel(Model):
    Id = IntegerField(null=False, primary_key=True)
    SeriesId = IntegerField(null=False)
    SeasonNumber = IntegerField(null=False)
    EpisodeNumber = IntegerField(null=False)
    Title = TextField()
    Overview = TextField()
    EpisodeFileId = IntegerField()
    AbsoluteEpisodeNumber = IntegerField()
    SceneAbsoluteEpisodeNumber = IntegerField()
    SceneSeasonNumber = IntegerField()
    SceneEpisodeNumber = IntegerField()
    Monitored = BooleanField()
    AirDateUtc = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    AirDate = TextField()
    Ratings = TextField()
    Images = TextField()
    UnverifiedSceneNumbering = BooleanField(null=False, default=False)
    LastSearchTime = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    AiredAfterSeasonNumber = IntegerField()
    AiredBeforeSeasonNumber = IntegerField()
    AiredBeforeEpisodeNumber = IntegerField()
    TvdbId = IntegerField()
    Runtime = IntegerField()
    FinaleType = TextField()


class SeriesModel(Model):
    Id = IntegerField(null=False, primary_key=True)
    TvdbId = IntegerField()
    TvRageId = IntegerField()
    ImdbId = TextField()
    Title = TextField()
    TitleSlug = TextField()
    CleanTitle = TextField()
    Status = IntegerField()
    Overview = TextField()
    AirTime = TextField()
    Images = TextField()
    Path = TextField()
    Monitored = BooleanField()
    SeasonFolder = IntegerField()
    LastInfoSync = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    LastDiskSync = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    Runtime = IntegerField()
    SeriesType = IntegerField()
    Network = TextField()
    UseSceneNumbering = BooleanField()
    FirstAired = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    NextAiring = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    Year = IntegerField()
    Seasons = TextField()
    Actors = TextField()
    Ratings = TextField()
    Genres = TextField()
    Certification = TextField()
    SortTitle = TextField()
    QualityProfileId = IntegerField()
    Tags = TextField()
    Added = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    AddOptions = TextField()
    TvMazeId = IntegerField()
    OriginalLanguage = IntegerField()


class SeriesModelv4(Model):
    Id = IntegerField(null=False, primary_key=True)
    TvdbId = IntegerField()
    TvRageId = IntegerField()
    ImdbId = TextField()
    Title = TextField()
    TitleSlug = TextField()
    CleanTitle = TextField()
    Status = IntegerField()
    Overview = TextField()
    AirTime = TextField()
    Images = TextField()
    Path = TextField()
    Monitored = BooleanField()
    SeasonFolder = IntegerField()
    LastInfoSync = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    LastDiskSync = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    Runtime = IntegerField()
    SeriesType = IntegerField()
    Network = TextField()
    UseSceneNumbering = BooleanField()
    FirstAired = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    NextAiring = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    Year = IntegerField()
    Seasons = TextField()
    Actors = TextField()
    Ratings = TextField()
    Genres = TextField()
    Certification = TextField()
    SortTitle = TextField()
    QualityProfileId = IntegerField()
    Tags = TextField()
    Added = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
    AddOptions = TextField()
    TvMazeId = IntegerField()
    OriginalLanguage = IntegerField()
    LastAired = DateTimeField(formats=["%Y-%m-%d %H:%M:%S.%f"])
