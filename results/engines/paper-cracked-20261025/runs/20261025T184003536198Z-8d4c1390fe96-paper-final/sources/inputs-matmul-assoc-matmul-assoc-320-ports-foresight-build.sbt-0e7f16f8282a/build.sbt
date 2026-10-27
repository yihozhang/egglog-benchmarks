// sbt project for the foresight ports of this suite: every <name>.scala in this directory is a
// port whose main object is bench.<name with '-' replaced by '_'>. Built inside the foresight
// image (systems/foresight/Containerfile) against the locally published foresight "bench" and
// its examples module (the port uses foresight's own example language and rules).
ThisBuild / scalaVersion := sys.env.getOrElse("FORESIGHT_SCALA", "3.4.1")

lazy val ports = (project in file("."))
  .settings(
    name := "foresight-ports",
    Compile / unmanagedSourceDirectories := Seq(baseDirectory.value),
    libraryDependencies ++= Seq(
      "com.github.jonathanvdc" %% "foresight" % sys.env.getOrElse("FORESIGHT_VERSION", "bench"),
      "com.github.jonathanvdc" %% "foresight-examples" % sys.env.getOrElse("FORESIGHT_VERSION", "bench"),
    ),
    scalacOptions ++= Seq("-deprecation", "-feature"),
  )
